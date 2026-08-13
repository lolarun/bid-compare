"""design/26 §9 —— qty_missing_rows 信号：compute_quality 里 not_evaluable
行此前从算术自洽分母里被静默 continue 掉，从不出现在 blocking/review_hints
里。qty 是这轮唯一保留 96% 硬指标的字段（qty×单价=合价，误差会传导到评标
总价），"读不出数量"不能是无声的——这里单独验证它跟 not_quoted（原文明确
不报价，合法）分得开、不误伤。
"""
from __future__ import annotations

from apps.api.intelligence.extraction_draft import DraftRow, SourceRef, compute_quality


def _row(i: int, **fields) -> DraftRow:
    base = {"seq": str(i + 1), "qty": 10, "unit_price": 5, "total_price": 50}
    base.update(fields)
    return DraftRow(
        row_index=i, row_type="quote_line", raw_cells={},
        fields=base, source_ref=SourceRef(page=1, table=0, row=i + 1),
    )


def test_missing_qty_row_surfaces_as_review_hint():
    rows = [_row(0), _row(1, qty=None)]
    q = compute_quality(rows, page_metrics=[], total_pages=1, target_pages=[1])
    assert any(r.startswith("qty_missing_rows=") for r in q.blocking_reasons), q.blocking_reasons
    assert "qty_missing_rows=1" in q.blocking_reasons


def test_not_quoted_row_missing_qty_is_not_counted():
    """原文明确「不报价」（/、无）的行没有数量是合法的，不能跟"读不出"混为一谈。"""
    rows = [
        _row(0),
        _row(1, qty=None, unit_price=None, total_price=None, not_quoted=True),
    ]
    q = compute_quality(rows, page_metrics=[], total_pages=1, target_pages=[1])
    assert not any(r.startswith("qty_missing_rows=") for r in q.blocking_reasons), q.blocking_reasons


def test_all_qty_present_no_hint():
    rows = [_row(0), _row(1)]
    q = compute_quality(rows, page_metrics=[], total_pages=1, target_pages=[1])
    assert not any(r.startswith("qty_missing_rows=") for r in q.blocking_reasons), q.blocking_reasons
