"""docs/design/26 §5（轨 P1）：PaddleOCR-VL 报价适配器测试。

不依赖网络、不依赖 outputs/（gitignore，非受控产物）——全部用手搭的小型
`pages[].tables[].{cells[],matrix[]}` 结构，覆盖本轮实测发现并修复的几个真实缺陷：

- `_resolve_matrix`：matrix 是 cells[] 下标，不是文字本身
- `_locate_tax_rate_idx`：税率"NN%"形状做逐行独立锚点，命中不止一个时不可信
- `_parse_rate`：百分号转小数（core.utils.parse_num 不认 "%"，直接用会静默丢税率）
- `_classify_trailing_cells`：qty=1 时含税单价与含税合计数值相同的老歧义——
  trailing 只有一个数字候选时只认合计，不臆造一个单价（泰科龙实测：89 行表只有
  "价税合计"一列，没有独立"单价含税"列，误判过一次）
- `_extract_row_fields`：seq/name/spec 每行都要按位置取，不能只在兜底分支里取
  （曾经导致同一份文档内 seq 有一搭没一搭，下游 use_content_align 判据两头不讨好）
- `build_quote_csv`：续页表头复用受限于**相邻页范围**（不设上限会把几十页后
  完全无关的规格参考表当成报价表续页一路吃进来，泰科龙实测 recall 12.4%→100%）；
  找不到报价表返回 None，不产出空壳 CSV
"""
from __future__ import annotations

from apps.api.intelligence.paddle_vl import (
    _classify_trailing_cells,
    _extract_row_fields,
    _locate_tax_rate_idx,
    _parse_rate,
    _resolve_matrix,
    build_quote_csv,
)


def _cells(*texts: str) -> list[dict]:
    return [{"text": t} for t in texts]


def _table(header: list[str], rows: list[list[str]]) -> dict:
    """按 matrix=cells 下标的真实 Paddle 形状拼一张表。"""
    flat = header + [c for r in rows for c in r]
    cells = _cells(*flat)
    width = len(header)
    matrix = [list(range(0, width))]
    for i, _ in enumerate(rows):
        start = width + i * width
        matrix.append(list(range(start, start + width)))
    return {"cells": cells, "matrix": matrix}


def _doc(pages: list[tuple[int, list[dict]]]) -> dict:
    return {"pages": [{"page_num": n, "tables": tables} for n, tables in pages]}


# ─── §1 matrix 解析 ──────────────────────────────────────────────────────────

def test_resolve_matrix_reads_text_via_index_not_literal():
    table = {"cells": _cells("名称", "规格"), "matrix": [[0, 1]]}
    assert _resolve_matrix(table) == [["名称", "规格"]]


def test_resolve_matrix_empty_without_matrix():
    assert _resolve_matrix({"cells": _cells("x")}) == []


# ─── §2 税率行锚点 ───────────────────────────────────────────────────────────

def test_locate_tax_rate_idx_finds_unique_percent_cell():
    row = ["个", "1", "62.83", "62.83", "13%", "8.17", "71.00"]
    assert _locate_tax_rate_idx(row) == 4


def test_locate_tax_rate_idx_none_when_absent():
    row = ["个", "1", "62.83", "62.83"]
    assert _locate_tax_rate_idx(row) is None


def test_locate_tax_rate_idx_none_when_ambiguous():
    # 两个都形如百分比——不可信，交回调用方走退化路径，不猜哪个才是税率。
    row = ["13%", "个", "1", "62.83", "5%"]
    assert _locate_tax_rate_idx(row) is None


def test_parse_rate_percent_to_decimal():
    assert _parse_rate("13%") == 0.13


def test_parse_rate_plain_decimal_passthrough():
    assert _parse_rate("0.13") == 0.13


def test_parse_rate_empty_is_none():
    assert _parse_rate("") is None
    assert _parse_rate("abc") is None


# ─── §3 税额之后尾列：含税单价/含税合计的算术再认领 ────────────────────────────

def test_trailing_cells_two_numeric_candidates_assigns_both():
    # 凯硕真实场景：qty≠1，两个数字候选各自独立匹配到不同槽位。
    out = _classify_trailing_cells("62.83", "125.66", "13%", "16.34",
                                   ["70.998", "142.00", "KITZ"])
    assert out["unit_price_incl_tax"] == "70.998"
    assert out["total_price_incl_tax"] == "142.00"
    assert out["brand"] == "KITZ"


def test_trailing_cells_single_numeric_candidate_prefers_total_not_unit():
    # 泰科龙真实场景：qty=1，单价含税与价税合计数值相同，trailing 只有一个
    # 数字候选——该表本来就没有独立"单价含税"列，只报"价税合计"，不能两个
    # 槽位抢同一个值，golden 验证过应落在 total 不是 unit。
    out = _classify_trailing_cells("69.12", "69.12", "13%", "8.98",
                                   ["78.10", "伯尔梅特"])
    assert out.get("unit_price_incl_tax") is None
    assert out["total_price_incl_tax"] == "78.10"
    assert out["brand"] == "伯尔梅特"


def test_trailing_cells_unmatched_numeric_left_unassigned_not_guessed():
    # 算不出算术关系的数字列（比如乱入的编号）不得被当成 brand/remark 塞进去，
    # 也不该被硬凑成价格——留空，好过冒充一个不确定的映射。
    out = _classify_trailing_cells("10.00", "10.00", "13%", "1.30", ["999999"])
    assert "unit_price_incl_tax" not in out
    assert "total_price_incl_tax" not in out
    assert "brand" not in out


# ─── §4 逐行字段提取：seq 必须每行都取，不能只在兜底分支取 ──────────────────────

def test_extract_row_fields_seq_present_via_anchor_path():
    col_map = {0: "seq", 2: "name", 3: "spec"}
    row = ["1", "x", "Y型过滤器", "DN20", "个", "1", "62.83", "62.83", "13%", "8.17", "71.00"]
    fields = _extract_row_fields(col_map, row)
    assert fields["seq"] == "1"
    assert fields["name"] == "Y型过滤器"
    assert fields["qty"] == "1"
    assert fields["tax_rate"] == "0.13"


def test_extract_row_fields_no_anchor_falls_back_to_col_map():
    col_map = {0: "seq", 1: "name"}
    row = ["3", "合计"]
    fields = _extract_row_fields(col_map, row)
    assert fields["seq"] == "3"
    assert fields["name"] == "合计"


# ─── §5 整份 CSV 拼装：续页相邻页限制、无报价表返回 None ─────────────────────

_HEADER = ["序号", "名称", "规格", "单位", "数量", "单价", "合价", "税率", "税额", "价税合计", "品牌"]


def test_build_quote_csv_none_when_no_quote_table():
    doc = _doc([(0, [_table(["项目", "备注"], [["x", "y"]])])])
    assert build_quote_csv(doc) is None


def test_build_quote_csv_basic_row_roundtrip():
    rows = [["1", "阀门", "DN20", "个", "2", "10.00", "20.00", "13%", "2.60", "22.60", "KITZ"]]
    doc = _doc([(0, [_table(_HEADER, rows)])])
    csv_text = build_quote_csv(doc)
    assert csv_text is not None
    assert "阀门" in csv_text
    assert "22.60" in csv_text


def test_build_quote_csv_continuation_within_gap_is_absorbed():
    header_rows = [["1", "阀门A", "DN20", "个", "1", "10.00", "10.00", "13%", "1.30", "11.30", "KITZ"]]
    # 续页没有自己的表头行——直接是数据行，页码跟表头页相邻（gap=1）。
    cont_row = ["2", "阀门B", "DN25", "个", "1", "20.00", "20.00", "13%", "2.60", "22.60", "KITZ"]
    cont_table = {"cells": _cells(*cont_row), "matrix": [list(range(len(cont_row)))]}
    doc = _doc([
        (0, [_table(_HEADER, header_rows)]),
        (1, [cont_table]),
    ])
    csv_text = build_quote_csv(doc)
    assert "阀门A" in csv_text and "阀门B" in csv_text


def test_build_quote_csv_far_page_not_absorbed_as_continuation():
    header_rows = [["1", "阀门A", "DN20", "个", "1", "10.00", "10.00", "13%", "1.30", "11.30", "KITZ"]]
    # 一张跟报价表完全无关的规格参考表，出现在很远的后续页（gap 远超 3）——
    # 不能被当成续页吃进来（泰科龙实测复现：不设上限会把这类表一路吃到文档末尾）。
    unrelated_row = ["DN", "50", "65", "80", "100", "125", "150", "200", "250", "300", "350"]
    unrelated_table = {"cells": _cells(*unrelated_row), "matrix": [list(range(len(unrelated_row)))]}
    doc = _doc([
        (0, [_table(_HEADER, header_rows)]),
        (30, [unrelated_table]),
    ])
    csv_text = build_quote_csv(doc)
    assert "DN" not in csv_text.split("\n")[0]  # 表头没被这张无关表污染
    lines = [l for l in csv_text.splitlines()[1:] if l.strip()]
    assert len(lines) == 1  # 只有阀门A那一行，无关表的行没混进来
