"""招标采购清单的 VL 识别 —— 与报价共用解析与结构门，差异只在列。

夹具用虚构料号。形态复刻实测：两级表头、材质子列、价格列留空、序号即行轴。
"""
from __future__ import annotations

import pytest

from apps.api.intelligence.vl_direct import map_columns
from apps.api.intelligence.vl_tender import (
    TENDER_SLOTS,
    build_tender_draft,
    build_tender_fields,
)

# 金桥招标清单的实测形态：两级表头拍平成 父列_子列，价格列存在但为空
HEAD = ("row_type,序号,专业,项目名称,规格,型号,工作压力,材质_阀体,材质_阀芯,"
        "计量单位,数量,单价（元）不含税,page")


def _csv(*rows: str) -> str:
    return "\n".join([HEAD, *rows])


def _row(seq: int, name="闸阀", spec="DN100", body="球墨铸铁", core="不锈钢", qty=10):
    return f"detail,{seq},给排水,{name},{spec},Z45X-16Q,1.6Mpa,{body},{core},个,{qty},,1"


# ─── 与报价的三点实质差异 ────────────────────────────────────────────────────

def test_empty_price_columns_do_not_block():
    """采购清单是留给投标人填的**空表**，价格列存在但为空。

    报价侧「读到行却读不到钱」是 BLOCKED；招标侧照搬会把每一份都误判。
    """
    d = build_tender_draft(_csv(_row(1), _row(2)),
                           file_path="t.pdf", page_count=1, processed_pages=[1])
    assert d.quality.status != "BLOCKED"
    assert not any("no_price_column" in r for r in (d.quality.blocking_reasons or []))
    assert d.doc_type == "tender"


def test_two_level_header_collapses_into_materials():
    """「材质」跨阀体/阀芯/阀板…多个子列，必须收成一个字典而不是散落成平铺字段。"""
    d = build_tender_draft(_csv(_row(1)), file_path="t.pdf", page_count=1,
                           processed_pages=[1])
    assert d.rows[0].fields["materials"] == {"阀体": "球墨铸铁", "阀芯": "不锈钢"}


def test_empty_material_subcolumns_are_omitted_not_blanked():
    """不是每种阀门都有阀板。空子列不进字典——空字符串会被下游当成"材质是空"。"""
    d = build_tender_draft(_csv(_row(1, body="球墨铸铁", core="")),
                           file_path="t.pdf", page_count=1, processed_pages=[1])
    assert d.rows[0].fields["materials"] == {"阀体": "球墨铸铁"}


def test_sequence_is_the_row_axis_and_gaps_are_reported():
    """序号是比价矩阵唯一的行轴（CLAUDE.md §4）；缺口必须报出来。"""
    d = build_tender_draft(_csv(*[_row(i) for i in (1, 2, 4, 5)]),
                           file_path="t.pdf", page_count=1, processed_pages=[1])
    seq = d.meta["diagnostics"]["sequence"]
    assert seq["missing"] == [3]


# ─── 泛化：非阀门品类不得丢数据 ──────────────────────────────────────────────

def test_unknown_category_columns_survive_in_extra_fields():
    """**槽位表带着阀门色彩**（pressure 是阀门专有），桥架/风机盘管的关键属性
    都不在槽位里。它们必须原样留在 extra_fields，否则换个品类就静默丢数据。

    见 docs/design/22 §2.1 —— 这条是那份评审范围的第一项。
    """
    text = ("row_type,序号,专业,项目名称,规格型号,表面处理,板材厚度,荷载等级,"
            "计量单位,数量,page\n"
            "detail,1,电气,槽式桥架,200×100,热浸镀锌,1.5mm,轻型,米,120,1")
    d = build_tender_draft(text, file_path="t.pdf", page_count=1, processed_pages=[1])
    row = d.rows[0]
    assert row.fields["name"] == "槽式桥架" and row.fields["qty"] == 120.0
    assert row.extra_fields == {"表面处理": "热浸镀锌", "板材厚度": "1.5mm",
                                "荷载等级": "轻型"}
    assert len(row.raw_cells) == 11, "原始单元格一列都不能少"


def test_material_prefix_rule_is_not_tied_to_valve_subcolumn_names():
    """收「材质」子列靠**父列前缀**，不靠穷举阀体/阀芯——换品类子列名完全不同。"""
    text = ("row_type,序号,项目名称,材质_外壳,材质_内衬,数量,page\n"
            "detail,1,风管,镀锌钢板,玻璃棉,5,1")
    d = build_tender_draft(text, file_path="t.pdf", page_count=1, processed_pages=[1])
    assert d.rows[0].fields["materials"] == {"外壳": "镀锌钢板", "内衬": "玻璃棉"}


# ─── 槽位互斥 ────────────────────────────────────────────────────────────────

def test_one_column_cannot_fill_two_slots():
    """「规格型号」同时含「规格」和「型号」。无互斥时会同时落进 spec 和 model，
    下游看到两个字段值相同却不知道它们本是同一列。"""
    m = map_columns(["序号", "项目名称", "规格型号", "数量"], slots=TENDER_SLOTS)
    assert m.get("spec") == "规格型号"
    assert m.get("model") != "规格型号"


def test_separate_spec_and_model_columns_still_distinguished():
    """互斥不能矫枉过正：分列时两者仍要各归各位（金桥的实际形态）。"""
    m = map_columns(["序号", "项目名称", "规格", "型号", "数量"], slots=TENDER_SLOTS)
    assert m["spec"] == "规格" and m["model"] == "型号"


def test_tender_slots_carry_no_price_slot():
    """招标槽位表**不得有任何价格槽位** —— 清单里的价格列是空表，
    映射它等于把"投标人还没填"读成"报价为零"。"""
    price_slots = {"unit_price", "total_price",
                   "unit_price_excl_tax", "total_price_excl_tax",
                   "tax_rate", "tax_amount"}
    assert not (set(TENDER_SLOTS) & price_slots)


# ─── 字段构造 ────────────────────────────────────────────────────────────────

def test_build_tender_fields_never_invents_values():
    """空就是空。招标清单的缺失值不得被补齐——它是行轴的一部分，猜错就是串行。"""
    f = build_tender_fields(lambda _s: "", {}, {})
    assert f["name"] == "" and f["qty"] is None and f["materials"] == {}
