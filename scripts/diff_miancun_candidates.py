"""diff_miancun_candidates.py — 只读：输出绵存PDF/Golden差异行CSV及对应PDF页面截图。

不下结论、不修改golden、不阻塞Phase 2主验证。
纯诊断：把6条PDF-only、3条Excel-only行交人工核对。

运行：
  python scripts/diff_miancun_candidates.py
输出：
  outputs/diff_miancun/candidates.csv
  outputs/diff_miancun/page_<N>.png  （PDF-only行所在页面截图）
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SNAP    = REPO / "tests" / "fixtures" / "ocr_snapshots" / "quote_miancun.json"
PDF     = REPO / "docs" / "test" / "上海绵存投标文件.pdf"
GOLDEN  = REPO / "data" / "golden" / "quote_miancun.json"
OUT_DIR = REPO / "outputs" / "diff_miancun"


def _norm(s) -> str:
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
    return (_norm(name), _norm(spec), _norm(qty), _norm(total))


def main():
    if not SNAP.exists():
        print(f"[ERROR] 快照不存在: {SNAP}")
        sys.exit(1)

    os.environ["TABLE_GRID_DETERMINISTIC_MODE"] = "off"

    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter

    provider = SnapshotProvider(None, SNAP, mode="replay")
    draft = recognize_tables(str(PDF), provider, _get_quote_adapter())
    pdf_rows = [r for r in draft.rows if r.row_type == "quote_line"]

    golden_data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden_rows = golden_data.get("rows", [])

    def _pdf_key(r):
        f = r.fields
        tp = f.get("total_price_incl_tax") or f.get("total_price")
        return _make_key(f.get("name"), f.get("spec"), f.get("qty"), tp)

    def _golden_key(g):
        return _make_key(
            g.get("raw_name") or g.get("name"),
            g.get("spec"), g.get("qty"),
            g.get("total_price_incl_tax"),
        )

    golden_counter = Counter(_golden_key(g) for g in golden_rows)
    pdf_counter    = Counter(_pdf_key(r) for r in pdf_rows)

    # One-to-one Counter matching
    pdf_only: list = []
    remaining_golden = Counter(golden_counter)
    for r in pdf_rows:
        k = _pdf_key(r)
        if remaining_golden.get(k, 0) > 0:
            remaining_golden[k] -= 1
        else:
            pdf_only.append(r)

    golden_only: list = []
    remaining_pdf = Counter(pdf_counter)
    for g in golden_rows:
        k = _golden_key(g)
        if remaining_pdf.get(k, 0) > 0:
            remaining_pdf[k] -= 1
        else:
            golden_only.append(g)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── CSV ──────────────────────────────────────────────────────────────────
    csv_path = OUT_DIR / "candidates.csv"
    fieldnames = ["side", "page", "table", "row",
                  "name", "spec", "qty", "unit_price", "total_price",
                  "raw_name", "model", "brand", "note"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in pdf_only:
            fld = r.fields
            tp = fld.get("total_price_incl_tax") or fld.get("total_price")
            w.writerow({
                "side": "PDF-only",
                "page": r.source_ref.page,
                "table": r.source_ref.table,
                "row": r.source_ref.row,
                "name": fld.get("name"),
                "spec": fld.get("spec"),
                "qty": fld.get("qty"),
                "unit_price": fld.get("unit_price") or fld.get("unit_price_incl_tax"),
                "total_price": tp,
                "raw_name": fld.get("name"),
                "model": fld.get("model"),
                "brand": fld.get("brand"),
                "note": "待人工核对: PDF有但Excel Golden无",
            })
        for g in golden_only:
            w.writerow({
                "side": "Excel-only",
                "page": "",
                "table": "",
                "row": "",
                "name": g.get("name"),
                "spec": g.get("spec"),
                "qty": g.get("qty"),
                "unit_price": g.get("unit_price_incl_tax"),
                "total_price": g.get("total_price_incl_tax"),
                "raw_name": g.get("raw_name"),
                "model": g.get("model"),
                "brand": g.get("brand"),
                "note": "待人工核对: Excel有但PDF未提取到",
            })
    print(f"\n[CSV] {csv_path}")
    print(f"  PDF-only: {len(pdf_only)} 行, Excel-only: {len(golden_only)} 行")

    # ── 页面截图 ──────────────────────────────────────────────────────────────
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("\n[SKIP] PyMuPDF 未安装，跳过截图 (pip install pymupdf)")
        _print_summary(pdf_only, golden_only)
        return

    pages_needed = sorted({r.source_ref.page for r in pdf_only})
    doc = fitz.open(str(PDF))
    for pg in pages_needed:
        page = doc[pg - 1]          # fitz 0-based
        mat = fitz.Matrix(1.5, 1.5)  # 1.5x scale
        pix = page.get_pixmap(matrix=mat)
        img_path = OUT_DIR / f"page_{pg}.png"
        pix.save(str(img_path))
        print(f"  [PNG] {img_path}")
    doc.close()

    _print_summary(pdf_only, golden_only)


def _print_summary(pdf_only, golden_only):
    print(f"\n=== 差异候选汇总（不下结论，交人工核对）===")
    print(f"\nPDF-only（{len(pdf_only)} 行）：")
    for r in pdf_only:
        f = r.fields
        tp = f.get("total_price_incl_tax") or f.get("total_price")
        print(f"  page={r.source_ref.page} row={r.source_ref.row} "
              f"name={f.get('name')!r} spec={f.get('spec')!r} qty={f.get('qty')} total={tp}")

    print(f"\nExcel-only（{len(golden_only)} 行）：")
    for g in golden_only:
        print(f"  name={g.get('name')!r} raw_name={g.get('raw_name')!r} "
              f"spec={g.get('spec')!r} qty={g.get('qty')} "
              f"total={g.get('total_price_incl_tax')}")


if __name__ == "__main__":
    main()
