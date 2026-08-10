"""cable_baseline_report.py — 电缆四文件识别基线（纯快照重放，不调付费 API）。

按行类型分别计数，**不得把 invalid 当成召回**。同时报告 snapshot miss——
replay miss 必须显式暴露，不能静默降级。

用法：
    python scripts/cable_baseline_report.py            # 跟随当前环境的 ORIENT_V2
    ORIENT_V2=1 python scripts/cable_baseline_report.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SRC = REPO / "docs" / "test1" / "prj1"
SNAP = REPO / "tests" / "fixtures" / "ocr_snapshots"

DOCS = {
    "上海浦东": "quote_cable_pudong",
    "亨通": "quote_cable_hengtong",
    "宏胜": "quote_cable_hongsheng",
    "远东": "quote_cable_yuandong",
}


def main() -> int:
    from apps.api.core.enums import (
        RT_QUOTE_LINE, RT_INVALID, RT_SUBTOTAL, RT_GRAND_TOTAL,
    )
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter
    import apps.api.intelligence.table_recognizer as tr

    v2_env = os.getenv("ORIENT_V2")
    print(f"ORIENT_V2 环境变量 = {v2_env!r}   模块内 _ORIENT_V2 = {tr._ORIENT_V2}")
    print(f"{'文档':8}{'draft':>7}{'quote':>7}{'invalid':>8}{'subtot':>7}"
          f"{'grand':>7}{'其他':>6}  {'quality':<8}{'target':>7}{'failed':>8}{'miss':>6}")

    rows_out = []
    for name, slug in DOCS.items():
        snap = SNAP / f"{slug}.json"
        if not snap.exists():
            print(f"{name:8}  快照缺失：{snap}")
            rows_out.append({"doc": name, "error": "snapshot_missing"})
            continue
        provider = SnapshotProvider(inner=None, snapshot_path=snap, mode="replay")
        pdf = next(SRC.glob(f"*{name}.pdf"))
        try:
            draft = recognize_tables(file_path=str(pdf), provider=provider,
                                     adapter=_get_quote_adapter())
        except Exception as exc:
            print(f"{name:8}  重放失败：{type(exc).__name__}: {exc}")
            rows_out.append({"doc": name, "error": f"{type(exc).__name__}: {exc}"})
            continue

        counts = {}
        for r in draft.rows:
            counts[r.row_type] = counts.get(r.row_type, 0) + 1
        known = {RT_QUOTE_LINE, RT_INVALID, RT_SUBTOTAL, RT_GRAND_TOTAL}
        other = sum(v for k, v in counts.items() if k not in known)
        q = draft.quality
        misses = sum(getattr(provider, a, 0) for a in
                     ("_ocr_misses", "_llm_misses", "_meta_misses", "_visual_misses"))
        rec = {
            "doc": name,
            "draft_rows": len(draft.rows),
            "quote_line": counts.get(RT_QUOTE_LINE, 0),
            "invalid": counts.get(RT_INVALID, 0),
            "subtotal": counts.get(RT_SUBTOTAL, 0),
            "grand_total": counts.get(RT_GRAND_TOTAL, 0),
            "other": other,
            "quality_status": q.status,
            "target_pages": list(draft.target_pages),
            "failed_target_pages": list(getattr(q, "failed_target_pages", []) or []),
            "snapshot_misses": misses,
        }
        rows_out.append(rec)
        print(f"{name:8}{rec['draft_rows']:>7}{rec['quote_line']:>7}{rec['invalid']:>8}"
              f"{rec['subtotal']:>7}{rec['grand_total']:>7}{other:>6}  "
              f"{q.status:<8}{len(rec['target_pages']):>7}"
              f"{len(rec['failed_target_pages']):>8}{misses:>6}")

    tag = "v2on" if tr._ORIENT_V2 else "v2off"
    out = REPO / "tmp" / f"cable_baseline_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows_out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
