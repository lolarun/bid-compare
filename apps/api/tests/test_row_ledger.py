"""行数守恒台账 + 型号串归一化单元测试（doc/19 §L2、§L3）—— 无 OCR/LLM/API。

台账存在的理由：本轮 E2E 里远东 19 页丢了 14 页、招标清单 184 行进 92 行出，
全程零报错。台账要求每一个零产出/欠产出的页都带着原因出现。
"""
from __future__ import annotations

from apps.api.intelligence.extraction_draft import PageMetric, build_row_ledger
from apps.api.services.ingestion.standardize import normalize_model_code, standardize_name


def _m(page, expected, extracted, reason="", role="quote_table_continuation", rot=0):
    return PageMetric(
        page=page, page_index=page - 1, role=role,
        expected_rows=expected, extracted_rows=extracted,
        fallback_reason=reason, rotation_applied=rot,
    )


class TestRowLedger:
    def test_full_recall_reports_no_drops(self):
        metrics = [_m(1, 20, 20), _m(2, 22, 22)]
        led = build_row_ledger(metrics, [1, 2], recognized_rows=42)
        assert led.expected_rows == 42 and led.recognized_rows == 42
        assert led.dropped_rows == 0
        assert led.empty_pages == [] and led.short_pages == []

    def test_empty_page_is_recorded_with_reason(self):
        """远东场景：整页颗粒无收，必须带页号和原因进台账。"""
        metrics = [_m(3, 20, 11), _m(4, 22, 0, reason="no_table_structure", rot=90)]
        led = build_row_ledger(metrics, [3, 4], recognized_rows=11)
        assert [d.page for d in led.empty_pages] == [4]
        drop = led.empty_pages[0]
        assert drop.reason == "no_table_structure" and drop.expected == 22
        assert drop.rotation_applied == 90
        assert led.dropped_rows == 31

    def test_short_page_is_recorded_separately(self):
        metrics = [_m(3, 20, 11, reason="")]
        led = build_row_ledger(metrics, [3], recognized_rows=11)
        assert led.empty_pages == []
        assert [(d.page, d.lost, d.reason) for d in led.short_pages] == [(3, 9, "under_extracted")]

    def test_reason_is_never_blank(self):
        """没有具体原因也必须落一个兜底原因，不接受空字符串。"""
        metrics = [_m(5, 18, 0, reason="")]
        led = build_row_ledger(metrics, [5], recognized_rows=0)
        assert led.empty_pages[0].reason == "no_rows_extracted"

    def test_non_target_pages_are_excluded(self):
        metrics = [_m(1, 0, 0, role="cover"), _m(3, 20, 20)]
        led = build_row_ledger(metrics, [3], recognized_rows=20)
        assert led.target_pages == 1 and led.expected_rows == 20
        assert led.empty_pages == []

    def test_to_dict_is_serialisable_and_carries_reasons(self):
        metrics = [_m(4, 22, 0, reason="empty_html", rot=180)]
        d = build_row_ledger(metrics, [4], recognized_rows=0).to_dict()
        assert d["dropped_rows"] == 22
        assert d["empty_pages"][0]["reason"] == "empty_html"
        assert d["empty_pages"][0]["rotation_applied"] == 180


class TestModelCodeNormalization:
    """OCR 把型号串的字母游程拆开；电缆品类里型号是唯一匹配信号。"""

    def test_joins_letter_runs_split_by_ocr(self):
        assert normalize_model_code("RTTY Z-3*240+2*120") == "RTTYZ-3*240+2*120"
        assert normalize_model_code("Y FD-WDZA-Y JY-4*70+E35") == "YFD-WDZA-YJY-4*70+E35"

    def test_both_sides_converge(self):
        """招标锚点与 OCR 报价行经归一后必须相等，否则匹配无从谈起。"""
        assert (normalize_model_code("RTTY Z-3*240+2*120")
                == normalize_model_code("RTTYZ-3*240+2*120"))

    def test_letter_digit_boundary_joins(self):
        assert normalize_model_code("DN 50") == "DN50"

    def test_pure_digits_are_never_joined(self):
        """两个尺寸之间的空格必须保留，否则 300 150 会被粘成 300150。"""
        assert normalize_model_code("300 150") == "300 150"

    def test_chinese_adjacent_spaces_survive(self):
        assert normalize_model_code("闸阀 DN50") == "闸阀 DN50"
        assert normalize_model_code("矿物电缆 RTTY Z-4*35") == "矿物电缆 RTTYZ-4*35"

    def test_empty_input_is_safe(self):
        assert normalize_model_code("") == ""
        assert normalize_model_code(None) is None

    def test_standardize_name_preserves_original(self):
        out = standardize_name("RTTY Z-3*240+2*120")
        assert out["original"] == "RTTY Z-3*240+2*120"
        assert "RTTYZ" in out["standardized"]
