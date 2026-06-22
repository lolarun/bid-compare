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
    headers = [r for r in grid.rows if r.row_type == "section_header"]
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
    assert _classify_row(cells) == "invalid"


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
    assert "section_header" not in types_in_output
    assert "invalid" not in types_in_output


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


# ── 跨页表头继承 (inherited_header) ──────────────────────────────────────────

# 模拟绵存 p4 的 8 列表头
_MIANCUN_HEADER = [
    "材料(设备)名称", "规格型号", "质量标准技术指标", "计量单位", "数量", "单价", "合价", "备注"
]

_CONTINUATION_HTML = """<table>
<tr><td>不锈钢暗杆闸阀</td><td>DN40</td><td>W-Z15W-16P</td><td>个</td><td>1</td><td>338</td><td>338</td><td></td></tr>
<tr><td>不锈钢暗杆闸阀</td><td>DN50</td><td>W-Z15W-16P</td><td>个</td><td>1</td><td>421</td><td>421</td><td></td></tr>
<tr><td>暗杆软密封闸阀</td><td>DN65</td><td>E3243</td><td>个</td><td>35</td><td>850</td><td>29750</td><td></td></tr>
</table>"""


def test_inherited_header_used_for_headerless_continuation():
    """续表页无表头行，但列数与 inherited_header 精确匹配 → 生成有效 TableGrid。"""
    grids = html_to_table_grids(_CONTINUATION_HTML, page_num=5,
                                inherited_header=_MIANCUN_HEADER)
    assert len(grids) == 1
    g = grids[0]
    assert g.header == _MIANCUN_HEADER
    quote_lines = [r for r in g.rows if r.row_type == "quote_line"]
    assert len(quote_lines) == 3
    # col_map should resolve semantic slots correctly
    assert g.col_map.get("材料(设备)名称") == "name"
    assert g.col_map.get("数量") == "qty"
    assert g.col_map.get("单价") == "unit_price"
    assert g.col_map.get("合价") == "total_price"
    # First row cells
    first = quote_lines[0].cells
    assert first.get("材料(设备)名称") == "不锈钢暗杆闸阀"
    assert first.get("数量") == "1"
    assert first.get("合价") == "338"


def test_inherited_header_not_used_when_column_count_differs():
    """列数不匹配时，inherited_header 不生效 → 返回空列表（无法解析）。"""
    wrong_header = ["名称", "规格", "单位", "数量", "单价"]  # 5列，但表格8列
    grids = html_to_table_grids(_CONTINUATION_HTML, page_num=5,
                                inherited_header=wrong_header)
    # Either empty or parsed with its own header detection (which fails → empty)
    assert all(g.header != wrong_header for g in grids)


def test_inherited_header_not_used_when_own_header_exists():
    """页面自有表头时，inherited_header 被忽略（自有 header 优先）。"""
    own_header_html = """<table>
    <tr><th>材料名称</th><th>规格型号</th><th>单位</th><th>数量</th><th>单价</th></tr>
    <tr><td>截止阀</td><td>DN50</td><td>个</td><td>10</td><td>128</td></tr>
    </table>"""
    wrong_inherited = ["X", "Y", "Z", "W", "V"]  # 5 cols, matches table
    grids = html_to_table_grids(own_header_html, page_num=3,
                                inherited_header=wrong_inherited)
    assert len(grids) == 1
    # Should use the page's own header, not inherited
    assert "材料名称" in grids[0].header


def test_no_inherited_header_returns_empty_for_headerless():
    """无 inherited_header 时，无表头续表页返回空列表（原有行为不变）。"""
    grids = html_to_table_grids(_CONTINUATION_HTML, page_num=5)
    assert grids == []


# ── seq 槽位（Phase B 确定性路径） ─────────────────────────────────────────

def test_col_map_seq_slot():
    """序号列映射到 seq 槽。"""
    header = ["序号", "材料名称", "规格型号", "单位", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("序号") == "seq"


def test_col_map_seq_variants():
    """编号/项次 也映射到 seq。"""
    assert _map_columns(["编号", "名称", "数量"]).get("编号") == "seq"
    assert _map_columns(["项次", "名称", "数量"]).get("项次") == "seq"


def test_col_map_seq_anchored_not_remark():
    """锚定正则：'项目序号说明' 这类备注列不应被误判为 seq。"""
    header = ["项目序号说明", "材料名称", "数量"]
    col_map = _map_columns(header)
    assert col_map.get("项目序号说明") != "seq"


# ── model 槽 ────────────────────────────────────────────────

def test_col_map_model_slot_standalone():
    """型号（独立列名）→ model 槽；规格型号 → spec 槽（不被抢走）。"""
    header = ["序号", "材料名称", "规格型号", "型号", "单位", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("规格型号") == "spec"
    assert col_map.get("型号") == "model"


def test_col_map_model_slot_pinpai_xinghao():
    """品牌型号 → model 槽。"""
    header = ["名称", "品牌型号", "单位", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("品牌型号") == "model"


def test_col_map_spec_without_xinghaо():
    """规格列（无型号）→ spec；无 型号 列时 model 槽不被虚占。"""
    header = ["名称", "规格", "单位", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert col_map.get("规格") == "spec"
    assert "model" not in col_map.values()


def test_col_map_zhiliang_biaozhun_not_model():
    """质量标准技术指标 → 无匹配槽（不靠硬编码归入 model）。"""
    header = ["名称", "规格型号", "质量标准技术指标", "数量", "单价", "合价"]
    col_map = _map_columns(header)
    assert "质量标准技术指标" not in col_map   # extra_fields 保存，不抢 model 槽


# ── 通用列头扩展（Task 2 新增，覆盖采购表中的括号后缀变体） ────────────────

def test_col_map_name_cailiao_shebei():
    """材料(设备)、材料（设备）名称 → name 槽。"""
    for h in ["材料(设备)", "材料（设备）", "材料(设备)名称", "材料（设备）名称"]:
        cm = _map_columns([h, "数量", "单价", "合价"])
        assert cm.get(h) == "name", f"expected name for {h!r}, got {cm.get(h)!r}"


def test_col_map_unit_price_yuan_suffix():
    """单价(元) / 单价（元）→ unit_price 槽。"""
    for h in ["单价(元)", "单价（元）"]:
        cm = _map_columns(["名称", "数量", h, "合价(元)"])
        assert cm.get(h) == "unit_price", f"expected unit_price for {h!r}"


def test_col_map_total_price_yuan_suffix():
    """合价(元)/合计(元)/总价(元) → total_price 槽；含税合价(元) 同样。"""
    for h in ["合价(元)", "合计(元)", "总价(元)", "含税合价(元)", "含税合计(元)"]:
        cm = _map_columns(["名称", "数量", "单价", h])
        assert cm.get(h) == "total_price", f"expected total_price for {h!r}"


def test_col_map_qty_with_unit_suffix():
    """数量(个) / 数量（套）/ 数量（件）→ qty 槽。"""
    for h in ["数量(个)", "数量（套）", "数量（件）"]:
        cm = _map_columns(["名称", h, "单价", "合价"])
        assert cm.get(h) == "qty", f"expected qty for {h!r}"


def test_col_map_合价_and_含税合价_only_one_wins():
    """合价(元) 和 含税合价(元) 同时存在：先出现的夺得 total_price 槽，后出现的无法重复占槽。

    _map_columns 按 header 顺序首次匹配：合价(元) 在前 → 它胜出。
    这是已知行为；含税合价在前时的文档同理（先出现者胜）。
    """
    header = ["名称", "数量", "单价(元)", "合价(元)", "税率", "税额(元)", "含税合价(元)"]
    cm = _map_columns(header)
    # 先出现的 合价(元) 夺得 total_price
    assert cm.get("合价(元)") == "total_price"
    # 槽已被占，含税合价(元) 无法再映射同一槽
    assert cm.get("含税合价(元)") != "total_price"
    # total_price 槽只能被一个列头占用
    assert sum(1 for s in cm.values() if s == "total_price") == 1


def test_col_map_合计_standalone():
    """单独的 合计 → total_price（普通采购表常见列名）。"""
    cm = _map_columns(["名称", "规格", "单位", "数量", "单价", "合计", "备注"])
    assert cm.get("合计") == "total_price"


