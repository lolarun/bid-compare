"""audit_miancun_rows.py — 只读诊断：绵存PDF提取行数 vs Excel Golden 86行的候选差异。

不修改任何数据库、golden或快照。纯只读输出。

用途：
  用 Counter 多集合匹配找出候选差异行，分类为：
    - PDF真实但Excel没有
    - 小计/标题/空白等非报价行（被误判为 quote_line）
    - OCR或字段错位
    - 待人工确认

快照路径：tests/fixtures/ocr_snapshots/（与 run_baseline.py 一致）

运行：
  python scripts/audit_miancun_rows.py [--snapshot <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 统一快照路径（与 run_baseline.py / test_e2e_snapshot.py 一致）
DEFAULT_SNAP = REPO / "tests" / "fixtures" / "ocr_snapshots" / "quote_miancun.json"
PDF  = REPO / "docs" / "test" / "上海绵存投标文件.pdf"
GOLDEN = REPO / "data" / "golden" / "quote_miancun.json"


def _norm(s) -> str:
    """通用字段规范化：去空白小写；数字整数化（1.0→1 防 float/int 不匹配）。"""
    s_str = str(s or "").strip()
    if not s_str:
        return ""
    try:
        v = float(s_str.replace(",", "").replace("，", ""))
        return str(int(v)) if v == int(v) else str(round(v, 2))
    except (ValueError, TypeError):
        pass
    return "".join(s_str.split()).lower()


def _make_key(name, spec, qty, total) -> tuple:
    """可比较的行键（规范化）。用于 Counter 多集合匹配。"""
    return (_norm(name), _norm(spec), _norm(qty), _norm(total))


def main():
    parser = argparse.ArgumentParser(description="绵存PDF提取 vs Golden 行级审计（只读）")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAP),
                        help="快照 JSON 路径（默认 tests/fixtures/ocr_snapshots/quote_miancun.json）")
    args = parser.parse_args()

    snap_path = Path(args.snapshot)
    if not snap_path.exists():
        print(f"[ERROR] 快照不存在: {snap_path}")
        print("       先跑: python scripts/run_baseline.py quote_miancun --record")
        sys.exit(1)
    if not GOLDEN.exists():
        print("[ERROR] Golden 不存在:", GOLDEN)
        sys.exit(1)

    # 确保 off 模式，不受灰度开关影响
    os.environ["TABLE_GRID_DETERMINISTIC_MODE"] = "off"

    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    provider = SnapshotProvider(None, snap_path, mode="replay")
    adapter  = _get_quote_adapter()
    draft    = recognize_tables(str(PDF), provider, adapter)

    pdf_rows = [r for r in draft.rows if r.row_type == "quote_line"]
    print(f"\n=== 绵存 PDF 提取 quote_line 行数: {len(pdf_rows)} ===")
    print(f"    声明总金额: {draft.quality.declared_total}")
    print(f"    质量门: {draft.quality.status}")
    print(f"    失败目标页: {draft.quality.failed_target_pages}")

    # ── 按页统计 ─────────────────────────────────────────────────────────────
    from collections import defaultdict
    by_page: dict[int, int] = defaultdict(int)
    for r in pdf_rows:
        by_page[r.source_ref.page] += 1
    print("\n  各页 quote_line 数:")
    for p in sorted(by_page):
        print(f"    page {p}: {by_page[p]} 行")

    # ── 载入 Golden ──────────────────────────────────────────────────────────
    golden_data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden_rows = golden_data.get("rows", [])
    print(f"\n=== Excel Golden 行数: {len(golden_rows)} ===")

    # ── Counter 多集合匹配（保留合法重复行语义） ─────────────────────────────
    def _pdf_key(r):
        f = r.fields
        tp = f.get("total_price_incl_tax") or f.get("total_price")
        return _make_key(f.get("name"), f.get("spec"), f.get("qty"), tp)

    def _golden_key(g):
        # raw_name = PDF OCR 原文（与 PDF rows 的 name 字段同源）
        return _make_key(
            g.get("raw_name") or g.get("name"),
            g.get("spec"),
            g.get("qty"),
            g.get("total_price_incl_tax"),
        )

    golden_counter = Counter(_golden_key(g) for g in golden_rows)
    pdf_counter    = Counter(_pdf_key(r) for r in pdf_rows)

    # 多集合差集：PDF有但Golden消耗不了（含重复次数）
    pdf_extra   = pdf_counter - golden_counter     # PDF 多出
    golden_extra = golden_counter - pdf_counter    # Golden 多出（PDF缺报）

    matched = sum((pdf_counter & golden_counter).values())
    print(f"\n  Counter匹配行: {matched}/{len(pdf_rows)} (PDF→Golden)")
    print(f"  PDF多出键种数: {len(pdf_extra)}  Golden多出键种数: {len(golden_extra)}")
    print(f"  PDF多出总行数: {sum(pdf_extra.values())}  Golden缺失总行数: {sum(golden_extra.values())}")

    if not pdf_extra:
        print("\n  [OK] 无多出行（Counter语义，含重复次数）")
    else:
        # 找到 PDF 中多出的实际行对象
        unmatched_rows: list = []
        remaining_golden = Counter(golden_counter)
        for r in pdf_rows:
            k = _pdf_key(r)
            if remaining_golden.get(k, 0) > 0:
                remaining_golden[k] -= 1
            else:
                unmatched_rows.append(r)

        print(f"\n=== 候选差异行详情（{len(unmatched_rows)} 条）===\n")
        for i, r in enumerate(unmatched_rows, 1):
            f = r.fields
            tp = f.get("total_price_incl_tax") or f.get("total_price")
            name_val = f.get("name") or ""

            # 分类猜测
            classification = "待人工确认"
            if not name_val.strip():
                classification = "小计/空白/标题行（name 为空）"
            elif any(kw in name_val for kw in ["小计", "合计", "总计", "说明", "备注"]):
                classification = "小计/标题行（name 含关键词）"
            else:
                # 找名称相近的 golden 行（用于判断是否字段错位）
                name_n = _norm(name_val)
                best_g = next(
                    (g for g in golden_rows if _norm(g.get("name") or g.get("raw_name") or "") == name_n),
                    None
                )
                if best_g is None:
                    classification = "PDF有但Excel无（新增行或OCR多余识别）"
                elif str(f.get("qty") or "") != str(best_g.get("qty") or ""):
                    classification = "字段错位或OCR差异（qty不匹配同名Golden行）"

            print(f"候选 #{i}")
            print(f"  page={r.source_ref.page} table={r.source_ref.table} row={r.source_ref.row}")
            print(f"  name       : {f.get('name')!r}")
            print(f"  spec       : {f.get('spec')!r}")
            print(f"  qty        : {f.get('qty')}")
            print(f"  unit_price : {f.get('unit_price') or f.get('unit_price_incl_tax')}")
            print(f"  total      : {tp}")
            rc = r.raw_cells
            print(f"  raw_cells  : {dict(list(rc.items())[:6])}")
            print(f"  row_type   : {r.row_type}  parser_mode: {f.get('parser_mode')}")
            print(f"  分类猜测   : {classification}")
            print()

    if golden_extra:
        print(f"\n=== Excel Golden 中PDF缺失的行（{sum(golden_extra.values())} 条）===")
        remaining_pdf = Counter(pdf_counter)
        for g in golden_rows:
            k = _golden_key(g)
            if remaining_pdf.get(k, 0) > 0:
                remaining_pdf[k] -= 1
            elif golden_extra.get(k, 0) > 0:
                print(f"  缺失: name={g.get('name')!r} qty={g.get('qty')} total={g.get('total_price_incl_tax')}")


if __name__ == "__main__":
    main()
