"""design/28 §4.1 同供应商 PDF+Excel 双投——reconcile_quote_items 只是给
既有 reconcile_anchors 做字段名适配，这里测的是适配层本身，不重复
test_tender_pdf_extract.py 里已经覆盖的 reconcile_anchors 核心对账逻辑。
"""
from __future__ import annotations

from apps.api.services.tender.source_reconcile import reconcile_quote_items


def test_material_field_maps_to_name():
    xlsx = [{"material": "电缆A", "spec": "4x50", "unit": "米", "qty": 100}]
    pdf = [{"material": "电缆A", "spec": "4x50", "unit": "米", "qty": 100}]
    result = reconcile_quote_items(xlsx, pdf)
    assert result["recommended_source"] == "both_consistent"
    assert not result["field_mismatches"]


def test_document_row_index_becomes_1based_seq():
    xlsx = [{"material": "a", "document_row_index": 0}, {"material": "b", "document_row_index": 1}]
    pdf = [{"material": "a", "document_row_index": 0}, {"material": "b", "document_row_index": 1}]
    result = reconcile_quote_items(xlsx, pdf)
    assert result["xlsx_count"] == 2
    assert result["pdf_count"] == 2
    assert not result["seq_missing_in_pdf"]
    assert not result["seq_missing_in_xlsx"]


def test_falls_back_to_position_when_no_document_row_index():
    xlsx = [{"material": "a"}, {"material": "b"}]
    pdf = [{"material": "a"}, {"material": "b"}]
    result = reconcile_quote_items(xlsx, pdf)
    assert not result["seq_missing_in_pdf"]
    assert not result["seq_missing_in_xlsx"]


def test_excel_primary_flags_pdf_only_row():
    """Excel 主、PDF 校核——PDF 独有的一行必须显式标出来，不能被吞掉。"""
    xlsx = [{"material": "a", "document_row_index": 0}]
    pdf = [{"material": "a", "document_row_index": 0}, {"material": "b", "document_row_index": 1}]
    result = reconcile_quote_items(xlsx, pdf)
    assert result["seq_missing_in_xlsx"] == ["2"]
    assert result["recommended_source"] == "excel"


def test_field_mismatch_surfaces_qty_difference():
    xlsx = [{"material": "a", "qty": 100, "document_row_index": 0}]
    pdf = [{"material": "a", "qty": 95, "document_row_index": 0}]
    result = reconcile_quote_items(xlsx, pdf)
    assert result["recommended_source"] == "excel"
    assert any(m["field"] == "数量" for m in result["field_mismatches"])


def test_name_field_used_when_material_absent():
    """理论上不会真的撞上（两条来源产出的条目都用 material），但适配层
    自己不该假设调用方一定传 material——name 存在时也要认。"""
    xlsx = [{"name": "a", "document_row_index": 0}]
    pdf = [{"name": "a", "document_row_index": 0}]
    result = reconcile_quote_items(xlsx, pdf)
    assert result["recommended_source"] == "both_consistent"
