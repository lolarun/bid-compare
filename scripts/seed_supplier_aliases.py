"""种子脚本：从 Supplier 和 ExtractionJob 数据初始化 supplier_aliases 表。

别名来源（优先级从高到低）：
  1. Supplier.name          → alias_type=legal_name  （权威全称）
  2. Supplier.short_name    → alias_type=short_name   （简称，若非空且与 name 不同）
  3. ExtractionJob.filename → alias_type=filename     （上传文件名，仅 context.supplier_id 有值时）
  4. ExtractionJob.result.supplier_name
                            → alias_type=historical   （LLM 从文件中识别的公司名）

只处理 type='quote' 且 status='done' 的 job。
对于 context.supplier_id 未设置的 job，跳过（无法确定归属）。

用法：
  python scripts/seed_supplier_aliases.py             # 真正写入
  python scripts/seed_supplier_aliases.py --dry-run   # 仅打印，不写库
  python scripts/seed_supplier_aliases.py --reset     # 先清空 supplier_aliases 再重建（危险！）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from apps.api.core.database import DB_PATH
from apps.api.models.supplier import Supplier
from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.supplier_alias import SupplierAlias, normalize_alias
from apps.api.models import (  # noqa: F401  ← 触发所有 ORM 注册
    Material, Project, Quote, BidAlignmentGroup, BidAlignmentItem,
    BidSubmission, BidQuoteLine, TenderListSession,
)
from apps.api.core.database import Base


def _make_alias(
    supplier_id: int,
    alias_text: str,
    alias_type: str,
    created_by: str,
    source_ref: str,
    confidence: float = 1.0,
) -> SupplierAlias | None:
    """构造 SupplierAlias 对象；若 alias_text 清洗后为空则返回 None。"""
    norm = normalize_alias(alias_text)
    if not norm:
        return None
    obj = SupplierAlias(
        supplier_id=supplier_id,
        alias=alias_text,
        normalized_alias=norm,
        alias_type=alias_type,
        active=1,
        confidence=confidence,
        created_by=created_by,
        source_reference=source_ref,
    )
    return obj


def _try_add(session, obj: SupplierAlias, dry_run: bool) -> str:
    """尝试写入别名；返回 'added' / 'dup' / 'dry'。"""
    if dry_run:
        return "dry"
    try:
        session.add(obj)
        session.flush()
        return "added"
    except IntegrityError:
        session.rollback()
        return "dup"


def main():
    parser = argparse.ArgumentParser(description="初始化 supplier_aliases 表")
    parser.add_argument("--dry-run", action="store_true", help="不写库，仅打印")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先清空 supplier_aliases 表再重建（危险：会丢失人工维护的别名）",
    )
    args = parser.parse_args()

    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()

    if args.reset:
        if args.dry_run:
            print("[dry-run] --reset 会清空 supplier_aliases，跳过执行")
        else:
            confirm = input("确认清空 supplier_aliases 表？输入 yes 继续：")
            if confirm.strip().lower() != "yes":
                print("已取消。")
                return
            session.execute(text("DELETE FROM supplier_aliases"))
            session.commit()
            print("supplier_aliases 已清空。")

    counters = {"added": 0, "dup": 0, "dry": 0, "skip": 0}

    # ─── 来源 1 & 2：Supplier.name / short_name ────────────────────────────────
    print("\n=== 来源 1/2: Supplier.name / short_name ===")
    suppliers = session.query(Supplier).all()
    for sup in suppliers:
        # legal_name
        obj = _make_alias(
            sup.id, sup.name, "legal_name",
            "system_init", f"suppliers.id={sup.id}",
        )
        if obj:
            result = _try_add(session, obj, args.dry_run)
            counters[result] += 1
            if result in ("added", "dry"):
                print(f"  [{result}] {sup.id} legal_name  {obj.normalized_alias!r}")

        # short_name（非空且与 name 不同）
        short = (sup.short_name or "").strip()
        if short and short != sup.name:
            obj2 = _make_alias(
                sup.id, short, "short_name",
                "system_init", f"suppliers.id={sup.id}",
            )
            if obj2:
                result = _try_add(session, obj2, args.dry_run)
                counters[result] += 1
                if result in ("added", "dry"):
                    print(f"  [{result}] {sup.id} short_name {obj2.normalized_alias!r}")

    if not args.dry_run:
        session.commit()

    # ─── 来源 3 & 4：ExtractionJob（quote 类型，context.supplier_id 有值）────────
    print("\n=== 来源 3/4: ExtractionJob filename / result.supplier_name ===")
    jobs = (
        session.query(ExtractionJob)
        .filter(ExtractionJob.type == "quote", ExtractionJob.status == "done")
        .all()
    )
    for job in jobs:
        context = job.context or {}
        supplier_id = context.get("supplier_id")
        if not supplier_id:
            counters["skip"] += 1
            continue

        # 来源 3: filename
        fname = (job.filename or "").strip()
        if fname:
            obj = _make_alias(
                supplier_id, fname, "filename",
                "system_init", f"job:{job.id}:{fname}",
                confidence=0.9,
            )
            if obj:
                result = _try_add(session, obj, args.dry_run)
                counters[result] += 1
                if result in ("added", "dry"):
                    print(f"  [{result}] sup={supplier_id} filename {obj.normalized_alias!r}  ({fname!r:.50})")

        # 来源 4: result.supplier_name
        result_data = job.result or {}
        raw_supplier = (result_data.get("supplier_name") or "").strip()
        if raw_supplier:
            obj = _make_alias(
                supplier_id, raw_supplier, "historical",
                "system_init", f"job:{job.id}:result.supplier_name",
                confidence=0.85,
            )
            if obj:
                add_result = _try_add(session, obj, args.dry_run)
                counters[add_result] += 1
                if add_result in ("added", "dry"):
                    print(
                        f"  [{add_result}] sup={supplier_id} historical "
                        f"{obj.normalized_alias!r}  ({raw_supplier!r:.50})"
                    )

        if not args.dry_run:
            session.commit()

    # ─── 汇总 ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    mode = "dry-run 模拟" if args.dry_run else "实际写入"
    print(f"完成（{mode}）")
    print(f"  已写入   : {counters['added']}")
    print(f"  重复跳过 : {counters['dup']}")
    print(f"  job 无归属: {counters['skip']}")
    if args.dry_run:
        print(f"  模拟条目 : {counters['dry']}")

    session.close()


if __name__ == "__main__":
    main()
