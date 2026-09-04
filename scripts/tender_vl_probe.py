"""tender_vl_probe.py — 招标采购清单 VL 识别的一次真实运行 + 对 Excel 基准打分。

基准来自 tests/fixtures/documents/金桥地体上盖项目-采购清单.xlsx（客户提供、已人工核对）。
只在**行数与序号**两个维度下结论：那是行轴，比价矩阵全靠它；
名称/规格属文本维度，Excel 与 PDF 是两个产物，不作准确率基准（同 C 层的口径）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apps.api.core.config import get_settings
from apps.api.intelligence.vl_tender import recognize_tender_vl

PDF = REPO / "tests/fixtures/documents/金桥地体上盖项目-招标文件.pdf"
XLSX = REPO / "tests/fixtures/documents/金桥地体上盖项目-采购清单.xlsx"


def golden_rows() -> list[dict]:
    import openpyxl
    ws = openpyxl.load_workbook(XLSX, read_only=True)["阀门"]
    rows = list(ws.iter_rows(values_only=True))
    out = []
    for r in rows[3:]:
        if r[0] is None or not str(r[0]).strip().isdigit():
            continue
        out.append({"seq": str(r[0]).strip(), "name": str(r[2] or "").strip(),
                    "spec": str(r[3] or "").strip(), "unit": str(r[11] or "").strip(),
                    "qty": r[12]})
    return out


def main() -> int:
    pages = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1 else [])] or None
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    cfg, prov = get_settings(), DashScopeOCRProvider()
    g = golden_rows()
    print(f"基准（Excel）：{len(g)} 行，序号 {g[0]['seq']}→{g[-1]['seq']}")
    print(f"送检页：{pages or '整份'}｜模型 {cfg.DASHSCOPE_QUOTE_VL_MODEL}")

    t0 = time.time()
    draft = recognize_tender_vl(
        str(PDF),
        vl_call=lambda i, p: prov.vl_extract_csv(i, p, model=cfg.DASHSCOPE_QUOTE_VL_MODEL),
        orient_call=lambda parts, p: prov.vl_extract_csv(
            [b for _t, b in parts], p, model=cfg.DASHSCOPE_QUOTE_ORIENT_MODEL,
            labels=[t for t, _b in parts]),
        target_pages=pages,
    )
    lines = [r for r in draft.rows if r.row_type == "quote_line"]
    diag = draft.meta.get("diagnostics") or {}
    seq_rep = diag.get("sequence") or {}
    got_seqs = [r.fields.get("seq") for r in lines]
    want = {r["seq"] for r in g}
    have = {s for s in got_seqs if s}
    print(f"\n用时 {time.time()-t0:.0f}s｜质量 {draft.quality.status}")
    print(f"明细行 {len(lines)} / 基准 {len(g)}｜表头 {diag.get('header')}")
    print(f"序号门 {seq_rep.get('verdict')} 覆盖 {seq_rep.get('coverage')} 缺口 {seq_rep.get('missing_count')}")
    print(f"序号命中 {len(want & have)}/{len(want)}｜多出 {sorted(have - want)[:8]}｜缺失 {sorted(want - have)[:8]}")
    print(f"旋转 {draft.meta.get('rotations')}｜未决 {draft.meta.get('orientation_unresolved')}")
    out = REPO / "tmp" / "tender_vl_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "rows": [{"seq": r.fields.get("seq"), "name": r.fields.get("name"),
                  "spec": r.fields.get("spec"), "qty": r.fields.get("qty"),
                  "unit": r.fields.get("unit"), "materials": r.fields.get("materials"),
                  "page": r.source_ref.page} for r in lines],
        "header": diag.get("header"), "sequence": seq_rep,
        "quality": draft.quality.status,
        "blocking_reasons": draft.quality.blocking_reasons,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"产物 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
