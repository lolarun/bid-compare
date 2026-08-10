"""record_cable_snapshots.py — 为电缆四文件录制 OCR/LLM 快照（一次性 API 成本）。

录完之后，方向纠正、去重、表头继承这类**确定性**逻辑就能秒级重放四份真实文档，
不必再每改一行跑 25 分钟的 fresh E2E（识别规则：「OCR、方向纠正、切片、LLM 抽取
的输入输出必须可快照重放」）。

参数化：文档、输出目录、并发都由命令行给出，脚本内不写死本机路径。
已存在的快照默认跳过，避免重复付费；--force 覆盖。

用法：
    python scripts/record_cable_snapshots.py                 # 四份全录
    python scripts/record_cable_snapshots.py --doc 远东      # 只录一份
    python scripts/record_cable_snapshots.py --force
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "docs" / "test1" / "prj1"
DEFAULT_OUT = REPO / "tests" / "fixtures" / "ocr_snapshots"

DOCS = {
    "上海浦东": "quote_cable_pudong",
    "亨通": "quote_cable_hengtong",
    "宏胜": "quote_cable_hongsheng",
    "远东": "quote_cable_yuandong",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", action="append", help="供应商名，可重复；省略则全部")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true", help="已存在也重录")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(REPO / "tmp" / "record_cable.log",
                                                      "w", encoding="utf-8")])

    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    targets = args.doc or list(DOCS)
    summary = []

    for name in targets:
        slug = DOCS.get(name)
        if not slug:
            print(f"!! 未知文档 {name}；可选：{list(DOCS)}")
            return 2
        snap = out / f"{slug}.json"
        if snap.exists() and not args.force:
            print(f"  skip {slug}（快照已存在，--force 可覆盖）")
            continue
        pdf = next(SRC.glob(f"*{name}.pdf"))
        print(f"  录制 {name} → {snap.name}")
        t0 = time.time()
        provider = SnapshotProvider(inner=DashScopeOCRProvider(),
                                    snapshot_path=snap, mode="record")
        try:
            draft = recognize_tables(file_path=str(pdf), provider=provider,
                                     adapter=_get_quote_adapter())
        except Exception as exc:
            print(f"  !! {name} 识别抛错：{type(exc).__name__}: {exc}")
            summary.append({"doc": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        finally:
            if hasattr(provider, "save"):
                provider.save()
        dt = time.time() - t0
        ledger = draft.ledger.to_dict() if draft.ledger else None
        rec = {
            "doc": name, "slug": slug, "seconds": round(dt, 1),
            "rows": len(draft.rows), "target_pages": draft.target_pages,
            "quality": draft.quality.status, "ledger": ledger,
        }
        summary.append(rec)
        print(f"     {len(draft.rows)} 行 / {dt:.0f}s / quality={draft.quality.status}")

    (REPO / "tmp" / "record_cable_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n快照 → {out}\n摘要 → tmp/record_cable_summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
