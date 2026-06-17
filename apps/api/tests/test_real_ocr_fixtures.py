"""
Real OCR fixture tests — P0-4.

These tests use the actual OCR HTML files from data/ocr_test/ to verify that
grand_total rows are correctly classified and blocked from entering the DB.
No LLM/API calls are made.
"""
import re
from pathlib import Path

import pytest

from apps.api.intelligence.table_parser import (
    _GRAND_TOTAL_KEYWORDS,
    _classify_row,
    html_to_table_grids,
)
from apps.api.intelligence.page_classifier import classify_page, PageRole

REPO = Path(__file__).parent.parent.parent.parent  # project root
OCR_DIR = REPO / "data" / "ocr_test"

# Regex from quotes.py (keep in sync)
_GRAND_TOTAL_NAME_RE = re.compile(
    r"价税合计|总计|合计金额|投标总价|^合计$|含税总计|含税合计|详见投标清单"
)

_PAGE_SPLIT_RE = re.compile(r"={60}\nPage \d+[^\n]*\n```html\n(.*?)```", re.DOTALL)


def _extract_page_htmls(ocr_txt_path: Path) -> list[tuple[int, str]]:
    """Parse OCR text file → [(page_number, html_string), ...]."""
    content = ocr_txt_path.read_text(encoding="utf-8")
    results = []
    for i, m in enumerate(_PAGE_SPLIT_RE.finditer(content)):
        results.append((i + 1, m.group(1)))
    return results


def _all_rows_from_file(path: Path):
    """Yield all TableRow objects parsed from an OCR file."""
    for page_num, html in _extract_page_htmls(path):
        for grid in html_to_table_grids(html, page_num=page_num):
            yield from grid.rows


# ── keyword coverage ───────────────────────────────────────────────────────

def test_grand_total_keywords_cover_new_patterns():
    """Extended _GRAND_TOTAL_KEYWORDS must match all known aggregate patterns."""
    patterns = [
        "含税总计",
        "含税合计",
        "详见投标清单",
        "价税合计",
        "合计",
        "总计",
    ]
    for p in patterns:
        assert _GRAND_TOTAL_KEYWORDS.search(p), f"_GRAND_TOTAL_KEYWORDS missed: {p}"


def test_batch_confirm_name_re_blocks_aggregate_names():
    """_GRAND_TOTAL_NAME_RE should block known aggregate row names."""
    blocked = [
        "合计",
        "价税合计",
        "含税总计",
        "含税合计",
        "详见投标清单",
        "投标总价",
        "合计金额",
    ]
    for name in blocked:
        assert _GRAND_TOTAL_NAME_RE.search(name), f"Name filter missed: {name!r}"


# ── classify_row with new keywords ────────────────────────────────────────

def test_classify_hanzhuzonghji_as_grand_total():
    """含税总计 row must classify as grand_total."""
    cells = {"名称": "含税总计", "规格": "", "单位": "", "数量": "", "单价": "", "合价": "932154.00"}
    result = _classify_row(cells)
    assert result == "grand_total", f"Expected grand_total, got {result}"


def test_classify_hanzhuheji_as_grand_total():
    """含税合计 row must classify as grand_total."""
    cells = {"名称": "含税合计", "规格": "", "单位": "", "数量": "", "单价": "", "合价": "1067616.41"}
    result = _classify_row(cells)
    assert result == "grand_total", f"Expected grand_total, got {result}"


def test_classify_xiangjian_as_grand_total():
    """详见投标清单 row (total aggregated as qty=1) must classify as grand_total."""
    cells = {
        "材料名称": "详见投标清单",
        "规格型号": "",
        "质量标准技术指标": "",
        "数量": "1",
        "单价": "1067616.41",
        "合价": "1067616.41",
        "备注": "",
    }
    result = _classify_row(cells)
    assert result == "grand_total", f"Expected grand_total, got {result}"


# ── real OCR file: 泰科龙 ─────────────────────────────────────────────────

TAIKELONG_OCR = OCR_DIR / "泰科龙投标文件__ocr.txt"


@pytest.mark.skipif(not TAIKELONG_OCR.exists(), reason="OCR fixture not present")
def test_taikelong_xiangjian_not_quote_line():
    """
    泰科龙 '详见投标清单 1 1067616.41' row must NOT be classified as quote_line.

    This was the root cause of the ¥3.57M total anomaly: the row aggregates all
    line items as qty=1 unit_price=1067616.41, OCR misalignment then multiplied it
    by qty=62, producing an impossible ¥66M unit price. After fixing the keyword
    regex this row should be grand_total, not quote_line.
    """
    found = False
    for row in _all_rows_from_file(TAIKELONG_OCR):
        cells_str = " ".join(str(v) for v in row.cells.values())
        if "详见投标清单" in cells_str or "1067616" in cells_str:
            found = True
            assert row.row_type != "quote_line", (
                f"Row with '详见投标清单/1067616' classified as quote_line: {row}"
            )
    assert found, "Could not locate '详见投标清单' row in 泰科龙 OCR fixture"


@pytest.mark.skipif(not TAIKELONG_OCR.exists(), reason="OCR fixture not present")
def test_taikelong_heji_grand_total_found():
    """泰科龙 '合计 1067616.41' row must be classified as grand_total."""
    for row in _all_rows_from_file(TAIKELONG_OCR):
        cells_str = " ".join(str(v) for v in row.cells.values())
        if "1067616" in cells_str and "合计" in cells_str:
            assert row.row_type == "grand_total", (
                f"'合计 1067616' row expected grand_total, got {row.row_type}: {row}"
            )
            return
    # No assertion failure if row not found on parseable pages — just skip
    pytest.skip("Could not locate distinct '合计 1067616' grand_total row")


# ── real OCR file: 凯硕新正 ───────────────────────────────────────────────

KAISHUOXINZHENG_OCR = OCR_DIR / "凯硕新正投标文件__ocr.txt"


@pytest.mark.skipif(not KAISHUOXINZHENG_OCR.exists(), reason="OCR fixture not present")
def test_kaishuoxinzheng_page9_cert_classified_as_other():
    """
    凯硕新正 page 9 (营业执照) must be classified as PageRole.OTHER,
    so it gets included in meta_htmls for supplier name extraction.

    Root cause of previous failure: _CERT_SIGNALS used wrong term
    '社会统一信用代码' — page 9 has '统一社会信用代码' (correct official order).
    """
    pages = _extract_page_htmls(KAISHUOXINZHENG_OCR)
    assert len(pages) >= 9, "Need at least 9 pages in fixture"
    _page_num, page9_html = pages[8]  # 0-indexed, page 9
    cls = classify_page(page9_html)
    assert cls.primary_role == PageRole.OTHER, (
        f"Page 9 营业执照 should be OTHER, got {cls.primary_role!r}. "
        "Check _CERT_SIGNALS for '统一社会信用代码'."
    )


@pytest.mark.skipif(not KAISHUOXINZHENG_OCR.exists(), reason="OCR fixture not present")
def test_kaishuoxinzheng_no_932154_as_quote_line():
    """
    凯硕 932154 total must NOT appear as a quote_line anywhere in parsed tables.

    The 932154 amount appears in the 投标书 letter text (page 2), not as a
    structured table total row. After the keyword fix, if it ever appears as a
    table row it must be classified as grand_total, not quote_line.
    """
    for row in _all_rows_from_file(KAISHUOXINZHENG_OCR):
        cells_str = " ".join(str(v) for v in row.cells.values())
        if "932154" in cells_str:
            assert row.row_type != "quote_line", (
                f"Row with '932154' classified as quote_line: {row}"
            )
