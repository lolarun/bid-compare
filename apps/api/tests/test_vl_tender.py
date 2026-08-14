"""招标采购清单的 VL 识别 —— 与报价共用解析与结构门，差异只在列。

夹具用虚构料号。形态复刻实测：两级表头、材质子列、价格列留空、序号即行轴。
"""
from __future__ import annotations

import pytest

from apps.api.intelligence.vl_quote import map_columns
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


# ─── 一份解析器，两个消费方 ──────────────────────────────────────────────────
#
# 招标（比价）与邀标对招标文件解析能力的要求**一致**：都要采购清单，也都要封面
# 四标量。给两条流程各写一个解析器，同一份 PDF 迟早会给出两种清单。

import re  # noqa: E402

from apps.api.intelligence.vl_tender import (  # noqa: E402
    parse_tender_document,
    parse_tender_meta,
)

_META_TEXT = ("project_name: 示例项目A标段\nproject_code: XX-2026-001\n"
              "tender_date: 2026-03-01\ndeadline: 2026-03-20")
_LIST_CSV = ("row_type,序号,项目名称,规格,计量单位,数量,page\n"
             "detail,1,闸阀,DN100,个,10,1\ndetail,2,蝶阀,DN50,个,20,1")


class _FakeVL:
    """按提示词区分四种调用：方向 / 清单 / 封面 / 要求。

    **必须区分全部四种**——少认一种就会把它算成另一种，测试看到的调用次数就是假的。
    """

    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def __call__(self, images, prompt, **_kw):
        if "key: value" in prompt:
            kind, out = "meta", _META_TEXT
        elif "###" in prompt:
            kind, out = "req", "### 材料类别\n水阀门"
        else:
            kind, out = "list", _LIST_CSV
        self.calls.append((kind, len(images)))
        return out

    def orient(self, parts, _prompt):
        self.calls.append(("orient", len(parts)))
        return "\n".join(f"{int(m.group(1))},0" for _l, _b in parts
                         for m in [re.match(r"PAGE_(\d+)_ROT_", _l)] if m)


@pytest.fixture
def tender_pdf():
    from pathlib import Path
    p = Path(__file__).resolve().parents[3] / "tests/fixtures/documents/tender/金桥地体上盖招标文件.pdf"
    if not p.exists():
        pytest.skip(f"招标夹具缺失：{p}")
    return str(p)


def test_one_parse_yields_both_list_and_cover_scalars(tender_pdf):
    vl = _FakeVL()
    r = parse_tender_document(tender_pdf, vl_call=vl, target_pages=[5])
    assert len(r.draft.rows) == 2
    assert r.meta["project_name"] == "示例项目A标段"
    assert r.meta["deadline"] == "2026-03-20"
    assert r.draft.meta["tender_meta"] == r.meta


def test_cover_pages_are_rendered_even_when_list_pages_are_pinned(tender_pdf):
    """指定清单页时封面通常不在其中；漏渲染它就等于静默丢掉四个标量。"""
    vl = _FakeVL()
    parse_tender_document(tender_pdf, vl_call=vl, target_pages=[5])
    kinds = dict(vl.calls)
    counts = {k: sum(1 for kk, _n in vl.calls if kk == k) for k, _ in vl.calls}
    assert kinds["list"] == 1, "清单只送指定的那一页"
    assert kinds["meta"] >= 1, "封面页必须另外渲染并送检"
    assert counts == {"list": 1, "meta": 1, "req": 1}, f"每类恰好一次，实际 {counts}"


def test_render_happens_once_for_both_extractions(tender_pdf):
    """清单与封面共用同一批图像——渲染两次是纯浪费，且两次结果可能不一致。"""
    from apps.api.intelligence import vl_tender as vt
    calls = {"n": 0}
    real = vt.DocumentLoader.render_pages

    def counting(path, pages):
        calls["n"] += 1
        return real(path, pages)

    vt.DocumentLoader.render_pages = staticmethod(counting)
    try:
        parse_tender_document(tender_pdf, vl_call=_FakeVL(), target_pages=[5])
    finally:
        vt.DocumentLoader.render_pages = staticmethod(real)
    assert calls["n"] == 1, f"渲染被调用 {calls['n']} 次，应为 1"


def test_meta_failure_does_not_sink_the_list(tender_pdf):
    """清单才是主线。封面读不出应当留空并可见，不该让整份识别失败。"""
    def flaky(images, prompt, **_kw):
        if "key: value" in prompt:
            raise RuntimeError("meta down")
        return _LIST_CSV

    r = parse_tender_document(tender_pdf, vl_call=flaky, target_pages=[5])
    assert len(r.draft.rows) == 2
    assert r.meta == {"project_name": "", "project_code": "",
                      "tender_date": "", "deadline": ""}


def test_meta_parser_ignores_unknown_lines_and_never_guesses():
    m = parse_tender_meta("project_name: A\n随便一行没有冒号\nunknown_key: B\ndeadline：C")
    assert m["project_name"] == "A" and m["deadline"] == "C"
    assert m["project_code"] == "" and m["tender_date"] == ""


# ─── 招标要求：可扩展 ────────────────────────────────────────────────────────
#
# 要求是**数据不是代码**：加「要求 N」= 加一条 TenderRequirement，不改解析逻辑、
# 不加模型调用。下面这组测试守的就是这条性质。

from apps.api.intelligence.vl_tender import (  # noqa: E402
    DEFAULT_TENDER_REQUIREMENTS,
    TenderRequirement,
    build_requirements_prompt,
    extract_tender_requirements,
    parse_requirements,
)

_REQ_TEXT = """### 业主品牌要求
brand_en,brand_cn
ALFA,阿法
VEGA,威盖

### 投标单位参与品牌
supplier_name,brand
星辉（上海）机电设备科技有限公司,阿法

### 材料类别
水阀门"""


def test_parses_table_and_text_requirements():
    out = parse_requirements(_REQ_TEXT, DEFAULT_TENDER_REQUIREMENTS)
    assert out["brand_requirement"] == [{"brand_en": "ALFA", "brand_cn": "阿法"},
                                        {"brand_en": "VEGA", "brand_cn": "威盖"}]
    assert out["supplier_brands"] == [
        {"supplier_name": "星辉（上海）机电设备科技有限公司", "brand": "阿法"}]
    assert out["material_class"] == "水阀门"


def test_missing_requirement_is_empty_not_absent():
    """**缺的项留空而不是缺键** —— 否则下游分不清"没这一项"和"这次没读到"。"""
    out = parse_requirements("### 材料类别\n水阀门", DEFAULT_TENDER_REQUIREMENTS)
    assert out["brand_requirement"] == [] and out["supplier_brands"] == []
    assert set(out) == {r.key for r in DEFAULT_TENDER_REQUIREMENTS}


def test_adding_a_requirement_needs_no_parser_change():
    """加一项 = 加一条配置。这条测试就是那个契约本身。"""
    extra = TenderRequirement(key="warranty", title="质保期",
                              hint="质保年限", shape="text")
    reqs = (*DEFAULT_TENDER_REQUIREMENTS, extra)
    assert "质保期" in build_requirements_prompt(reqs)
    out = parse_requirements(_REQ_TEXT + "\n\n### 质保期\n两年", reqs)
    assert out["warranty"] == "两年"
    assert out["material_class"] == "水阀门", "新增项不得干扰既有项"


def test_all_requirements_come_from_one_model_call():
    """逐项调用等于把同样的图片重复送 N 遍；且分开问容易让模型混淆
    "业主要求的品牌"与"各家申报的品牌"。"""
    calls = []

    def vl(images, prompt, **_kw):
        calls.append(len(images))
        return _REQ_TEXT

    extract_tender_requirements([b"a", b"b"], vl, DEFAULT_TENDER_REQUIREMENTS)
    assert calls == [2], f"应只调用一次，实际 {len(calls)} 次"


def test_unknown_sections_are_ignored_and_nothing_is_guessed():
    out = parse_requirements("### 不认识的东西\n随便\n### 材料类别\n水阀门",
                             DEFAULT_TENDER_REQUIREMENTS)
    assert out["material_class"] == "水阀门"
    assert out["brand_requirement"] == []


def test_requirement_failure_leaves_every_key_empty():
    """要求读不出应当留空且可见——清单才是主线，不该让整份识别失败。"""
    def boom(_images, _prompt, **_kw):
        raise RuntimeError("down")

    out = extract_tender_requirements([b"x"], boom, DEFAULT_TENDER_REQUIREMENTS)
    assert out == {"brand_requirement": [], "supplier_brands": [], "material_class": ""}


def test_prompt_contains_no_real_supplier_or_project_names():
    """生产 prompt 禁止出现真实供应商/项目/文件名（.claude/rules/recognition.md）。"""
    text = build_requirements_prompt(DEFAULT_TENDER_REQUIREMENTS)
    for forbidden in ("金桥", "凯硕", "泰科龙", "绵存", "宏胜", "亨通", "远东", "浦东"):
        assert forbidden not in text
