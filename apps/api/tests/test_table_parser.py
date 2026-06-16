"""Tests for apps/api/intelligence/table_parser.py"""
import json
import pytest
from pathlib import Path

from apps.api.intelligence.table_parser import (
    html_to_table_grids,
    grids_to_llm_json,
    _expand_table_rows,
    _detect_header,
    _map_columns,
    _classify_row,
    TableGrid,
    TableRow,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ── html_to_table_grids ────────────────────────────────────────────────────

def test_basic_quote_table_page():
    """quote_table_page.html: 5 quote_lines, 1 header row."""
    html = (FIXTURES / "quote_table_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=1)
    assert len(grids) == 1
    grid = grids[0]
    assert grid.page == 1
    assert grid.table_index == 0
    quote_lines = [r for r in grid.rows if r.row_type == "quote_line"]
    headers = [r for r in grid.rows if r.row_type == "header"]
    assert len(quote_lines) == 5
    assert len(headers) >= 1


def test_quote_last_page_grand_total():
    """quote_last_page.html: 3 quote_lines + 1 grand_total row."""
    html = (FIXTURES / "quote_last_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=2)
    assert len(grids) == 1
    grid = grids[0]
    grand_totals = [r for r in grid.rows if r.row_type == "grand_total"]
    quote_lines = [r for r in grid.rows if r.row_type == "quote_line"]
    assert len(grand_totals) >= 1
    assert len(quote_lines) == 3


def test_non_table_html_returns_empty():
    """Certificate or cover page HTML without tables → empty list."""
    html = "<html><body><p>资质证书</p><p>有效期：2026</p></body></html>"
    grids = html_to_table_grids(html, page_num=3)
    assert grids == []


def test_empty_or_whitespace_html():
    grids = html_to_table_grids("", page_num=1)
    assert grids == []
    grids = html_to_table_grids("   \n  ", page_num=1)
    assert grids == []


def test_too_few_columns_ignored():
    """Table with < 3 columns should be skipped."""
    html = "<table><tr><th>名称</th><th>数量</th></tr><tr><td>截止阀</td><td>10</td></tr></table>"
    grids = html_to_table_grids(html, page_num=1)
    assert grids == []


def test_page_number_preserved():
    html = (FIXTURES / "quote_table_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=7)
    assert grids[0].page == 7


# ── rowspan / colspan expansion ────────────────────────────────────────────

def test_rowspan_colspan_expansion():
    """Table with merged cells: expand correctly."""
    html = """<table>
    <tr><th colspan="2">名称规格</th><th>数量</th><th>单价</th><th>合价</th></tr>
    <tr><td>截止阀</td><td>DN50</td><td>10</td><td>128.50</td><td>1285.00</td></tr>
    <tr><td rowspan="2">闸阀</td><td>DN80</td><td>5</td><td>256.00</td><td>1280.00</td></tr>
    <tr><td>DN100</td><td>3</td><td>380.00</td><td>1140.00</td></tr>
    </table>"""
    grids = html_to_table_grids(html, page_num=1)
    # Should parse without crash; rowspan rows get carried text
    assert len(grids) == 1
    # Row with rowspan: both rows should have something in the "阀" column
    quote_lines = [r for r in grids[0].rows if r.row_type == "quote_line"]
    assert len(quote_lines) >= 2


def test_expand_table_rows_basic():
    """Unit test for _expand_table_rows with colspan."""
    raw_rows = [
        [{"text": "A", "colspan": 2, "rowspan": 1}, {"text": "B", "colspan": 1, "rowspan": 1}],
        [{"text": "X", "colspan": 1, "rowspan": 1}, {"text": "Y", "colspan": 1, "rowspan": 1}, {"text": "Z", "colspan": 1, "rowspan": 1}],
    ]
    expanded = _expand_table_rows(raw_rows)
    assert expanded[0] == ["A", "A", "B"]
    assert expanded[1] == ["X", "Y", "Z"]


def test_expand_table_rows_rowspan():
    """Unit test for _expand_table_rows with rowspan."""
    raw_rows = [
        [{"text": "Header", "colspan": 1, "rowspan": 2}, {"text": "B", "colspan": 1, "rowspan": 1}],
        [{"text": "C", "colspan": 1, "rowspan": 1}],
    ]
    expanded = _expand_table_rows(raw_rows)
    assert expanded[0][0] == "Header"
    assert expanded[1][0] == "Header"  # carried from rowspan
    assert expanded[1][1] == "C"


# ── column mapping ─────────────────────────────────────────────────────────

def test_col_map_standard_headers():
    header = ["序号", "材料名称", "规格型号", "单位", "数量", "含税单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("材料名称") == "name"
    assert col_map.get("规格型号") == "spec"
    assert col_map.get("含税单价") == "unit_price"
    assert col_map.get("合价") == "total_price"
    assert col_map.get("数量") == "qty"
    assert col_map.get("单位") == "unit"


def test_col_map_incl_excl_tax_separation():
    """不含税单价 must NOT be mapped to unit_price slot."""
    header = ["名称", "规格型号", "单位", "数量", "不含税单价", "含税单价", "价税合计"]
    col_map = _map_columns(header)
    assert col_map.get("不含税单价") == "unit_price_excl_tax"
    assert col_map.get("含税单价") == "unit_price"
    assert col_map.get("价税合计") == "total_price"


def test_col_map_material_type():
    header = ["名称", "规格", "材质", "单位", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("材质") == "material_type"


# ── row type classification ────────────────────────────────────────────────

def test_classify_grand_total():
    cells = {"名称": "合计", "规格": "", "单位": "", "数量": "", "含税单价": "", "合价": "15030.00"}
    assert _classify_row(cells) == "grand_total"


def test_classify_price_tax_grand_total():
    cells = {"名称": "价税合计", "规格": "", "单位": "", "数量": "", "单价": "", "总价": "168000.00"}
    assert _classify_row(cells) == "grand_total"


def test_classify_subtotal():
    cells = {"名称": "阀门小计", "规格": "", "单位": "", "数量": "", "单价": "", "合价": "9000.00"}
    assert _classify_row(cells) == "subtotal"


def test_classify_empty():
    cells = {"名称": "", "规格": "", "单位": "", "数量": "", "单价": ""}
    assert _classify_row(cells) == "empty"


def test_classify_quote_line():
    cells = {"名称": "截止阀", "规格型号": "DN50 PN16", "单位": "个", "数量": "10",
             "含税单价": "128.50", "合价": "1285.00"}
    assert _classify_row(cells) == "quote_line"


# ── grids_to_llm_json ──────────────────────────────────────────────────────

def test_grids_to_llm_json_structure():
    html = (FIXTURES / "quote_table_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=5)
    result_str = grids_to_llm_json(grids)
    result = json.loads(result_str)

    assert "tables" in result
    tables = result["tables"]
    assert len(tables) == 1
    t = tables[0]
    assert t["page"] == 5
    assert t["table_index"] == 0
    assert "rows" in t

    # All rows in llm output should have row_index, row_type, cells
    for row in t["rows"]:
        assert "row_index" in row
        assert "row_type" in row
        assert "cells" in row

    # header rows should NOT appear in llm output (filtered out)
    types_in_output = {r["row_type"] for r in t["rows"]}
    assert "header" not in types_in_output
    assert "empty" not in types_in_output


def test_grids_to_llm_json_no_grand_total_rows():
    """grand_total rows appear in llm output (row_type visible) but not as quote_line."""
    html = (FIXTURES / "quote_last_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=2)
    result = json.loads(grids_to_llm_json(grids))
    rows = result["tables"][0]["rows"]
    grand_total_rows = [r for r in rows if r["row_type"] == "grand_total"]
    quote_line_rows = [r for r in rows if r["row_type"] == "quote_line"]
    assert len(grand_total_rows) >= 1
    assert len(quote_line_rows) == 3


def test_row_index_is_original_position():
    """row_index in output should reflect original position in HTML table."""
    html = (FIXTURES / "quote_table_page.html").read_text(encoding="utf-8")
    grids = html_to_table_grids(html, page_num=1)
    result = json.loads(grids_to_llm_json(grids))
    row_indices = [r["row_index"] for r in result["tables"][0]["rows"]]
    # Indices should be increasing (no duplicates)
    assert row_indices == sorted(row_indices)
    assert len(set(row_indices)) == len(row_indices)
