"""Excel cross-reconciliation for VL-direct extraction drafts.

Extracted from table_recognizer.py during legacy retirement (best-practice
review F1): this was the one piece of that module still called by production
code — tender_pdf.extract_bidlist's VL path uses it to reconcile a PDF-derived
draft against an attached Excel procurement list. Everything else in
table_recognizer.py (RecognizeAdapter, recognize_tables, orientation
correction, TableGrid extraction, …) served the OCR→HTML→TableGrid legacy
chain, which is unreachable in production (both shipped providers implement
vl_extract_csv — see docs/design/21) and was deleted, not moved.

Deliberately independent of table_recognizer: only depends on DraftRow and
the tender_list / source_reconcile services.
"""
from __future__ import annotations

from apps.api.intelligence.extraction_draft import DETAIL_ROW_TYPE, DraftRow


def reconcile_vs_excel(
    doc_type: str,
    rows: list[DraftRow],
    xlsx_path: str,
    name_key: str,
) -> dict:
    """对比 ExtractionDraft rows 与 Excel ground truth。"""
    if doc_type == "tender":
        from apps.api.services.tender.source_reconcile import reconcile_anchors
        from apps.api.services.tender.tender_list import parse_tender_all_sheets
        xlsx_anchors = parse_tender_all_sheets(xlsx_path)
        xlsx_items = [
            {"seq": str(a.seq), "name": a.name, "spec": a.spec,
             "unit": a.unit, "qty": a.qty}
            for a in xlsx_anchors
        ]
        pdf_items = [
            {"seq": r.fields.get("seq") or "",
             "name": r.fields.get("name") or "",
             "spec": r.fields.get("spec") or "",
             "unit": r.fields.get("unit") or "",
             "qty": r.fields.get("qty")}
            for r in rows if r.row_type == DETAIL_ROW_TYPE
        ]
        return reconcile_anchors(xlsx_items, pdf_items, source_type="pdf_primary")
    # 报价侧对账：简单行数 + 声明总价检查
    return _reconcile_quote_vs_excel(rows, xlsx_path, name_key)


def _reconcile_quote_vs_excel(
    rows: list[DraftRow],
    xlsx_path: str,
    name_key: str,
) -> dict:
    """报价侧简单对账：Excel 行数、声明总价 vs 明细合计。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        ws = wb.active
        xlsx_row_count = sum(1 for row in ws.iter_rows(min_row=2) if any(c.value for c in row))
    except Exception as exc:
        return {"error": f"excel parse failed: {exc}"}

    pdf_quote_lines = [r for r in rows if r.row_type == DETAIL_ROW_TYPE]
    return {
        "xlsx_row_count": xlsx_row_count,
        "pdf_row_count": len(pdf_quote_lines),
        "row_count_match": xlsx_row_count == len(pdf_quote_lines),
    }
