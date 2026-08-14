"""docs/design/26 P4 补（招标侧）：PaddleOCR-VL 招标清单适配器测试。

不依赖网络、不依赖 outputs/（gitignore，非受控产物）——除 §4 的一个真实回归
用例（手抄自金桥招标件离线核实，`outputs/baidu_unlimited_ocr/tender_jinqiao.json`
零成本文字层产物，不是网络调用）外全部用手搭的小型
`pages[].tables[].{cells[],matrix[]}` 结构，覆盖：

- 三行表头（标题行 + 两级列表头）剥离与下划线合并（"材质"+"阀体"→"材质_阀体"）
- 品牌要求表（同样含"序号"关键词）不被误判成采购清单续页
- 材质子列区间空单元格被 Paddle 压缩掉一格导致"单位/数量"整体左移——按税率
  列锚点重新定位（跟报价侧 `paddle_vl.py` 同一类缺陷、同一个修法，金桥实测
  复现：32 行数量整段丢失，锚点修复后为零）
"""
from __future__ import annotations

from apps.api.intelligence.paddle_tender import (
    _header_rate_anchor_offsets,
    _looks_like_tender_table,
    build_tender_csv,
)
from apps.api.intelligence.vl_quote import map_columns
from apps.api.intelligence.vl_tender import TENDER_SLOTS, build_tender_draft


def _cells(*texts: str) -> list[dict]:
    return [{"text": t} for t in texts]


def _table_from_rows(rows: list[list[str]]) -> dict:
    """按 matrix=cells 下标的真实 Paddle 形状拼一张表；每行宽度可以不一样
    （压缩缺陷测试要用到）。"""
    flat = [c for r in rows for c in r]
    cells = _cells(*flat)
    matrix = []
    offset = 0
    for r in rows:
        matrix.append(list(range(offset, offset + len(r))))
        offset += len(r)
    return {"cells": cells, "matrix": matrix}


def _doc(pages: list[tuple[int, list[dict]]]) -> dict:
    return {"pages": [{"page_num": n, "tables": tables} for n, tables in pages]}


# ─── §1 表头识别：数量列是唯一信号，不是宽松关键词 ────────────────────────────

def test_looks_like_tender_table_requires_qty_column():
    assert _looks_like_tender_table(["序号", "专业", "项目名称", "规格", "单位", "数量"])


def test_looks_like_tender_table_rejects_requirement_table_despite_seq_and_spec_substring():
    # "技术规格书"含"规格"子串、"序号"也在——但没有数量列，不是采购清单。
    header = ["序号", "材料类别", "业主招标品牌要求", "有无技术规格书", "本次邀请投标单位"]
    assert not _looks_like_tender_table(header)


# ─── §2 三行表头：标题行 + 两级列表头 ──────────────────────────────────────────

def test_title_row_stripped_and_two_level_header_merged_with_underscore():
    title = ["同一个项目全名"] * 8
    parent = ["序号", "项目名称", "规格", "单位", "数量", "材质", "材质", "材质"]
    child = ["序号", "项目名称", "规格", "单位", "数量", "阀体", "阀芯", "阀板"]
    data = [["1", "闸阀", "DN100", "个", "5", "球墨铸铁", "", ""]]
    doc = _doc([(0, [_table_from_rows([title, parent, child] + data)])])
    csv_text = build_tender_csv(doc)
    assert csv_text is not None
    header_line = csv_text.splitlines()[0]
    assert "材质_阀体" in header_line
    assert "材质_阀芯" in header_line
    data_line = csv_text.splitlines()[1]
    assert data_line.startswith("detail,1,闸阀,DN100")


# ─── §3 品牌要求表不被吸收成续页 ───────────────────────────────────────────────

def test_requirement_table_not_absorbed_as_bidlist_continuation():
    bidlist_header = ["序号", "项目名称", "规格", "单位", "数量"]
    bidlist_row = ["1", "闸阀", "DN100", "个", "5"]
    req_header = ["序号", "材料类别", "业主招标品牌要求", "有无技术规格书", "本次邀请投标单位"]
    req_row = ["1", "水阀门", "KITZ、WATTS", "有", "某某机电公司"]
    doc = _doc([
        (0, [_table_from_rows([bidlist_header, bidlist_row])]),
        (1, [_table_from_rows([req_header, req_row])]),
    ])
    csv_text = build_tender_csv(doc)
    rows = csv_text.splitlines()[1:]
    assert len(rows) == 1  # 品牌要求表那一行不应该被当成续页数据吃进来
    assert "某某机电公司" not in csv_text


# ─── §4 材质子列压缩缺陷：按税率锚点重新定位单位/数量 ──────────────────────────

def test_qty_survives_material_column_compression_via_rate_anchor():
    """金桥实测复现：材质 5 子列若某一行有一格是空的，`matrix` 少一格而不是
    补空占位，'单位/数量'这些排在材质区块之后的字段整体左移一位。"""
    header = ["序号", "项目名称", "规格", "材质_阀体", "材质_阀芯", "材质_阀板",
             "材质_阀杆", "材质_密封圈", "单位", "数量", "单价", "合计", "税率", "税额"]
    # 正常行：5 个材质格全部占位（即使是空字符串）
    normal_row = ["1", "闸阀", "DN100", "球墨铸铁", "", "", "", "", "个", "5",
                  "", "0.00", "13%", "0.00"]
    # 压缩行：材质区块少一格（只有 4 个位置，不是 5 个）——单位/数量整体左移
    compressed_row = ["2", "球阀", "DN80", "", "", "", "", "个", "3",
                      "", "0.00", "13%", "0.00"]
    doc = _doc([(0, [_table_from_rows([header, normal_row, compressed_row])])])
    csv_text = build_tender_csv(doc)
    draft = build_tender_draft(csv_text, file_path="x.pdf", page_count=1,
                               processed_pages=[1])
    by_seq = {r.fields.get("seq"): r.fields for r in draft.rows}
    assert by_seq["1"]["qty"] == 5.0
    assert by_seq["1"]["unit"] == "个"
    assert by_seq["2"]["qty"] == 3.0, by_seq["2"]
    assert by_seq["2"]["unit"] == "个"


def test_header_rate_anchor_offsets_empty_without_rate_column():
    header = ["序号", "项目名称", "规格", "单位", "数量"]
    col_map = _tender_col_map(header)
    assert _header_rate_anchor_offsets(header, col_map) == {}


def _tender_col_map(header):
    base = map_columns(header, slots=TENDER_SLOTS)
    idx_of = {h: i for i, h in enumerate(header)}
    return {idx_of[h]: slot for slot, h in base.items() if h in idx_of}


# ─── §5 没有清单表时返回 None，不产出空壳 ──────────────────────────────────────

def test_build_tender_csv_none_when_no_tender_table():
    doc = _doc([(0, [_table_from_rows([["备注"], ["随便写点什么"]])])])
    assert build_tender_csv(doc) is None
