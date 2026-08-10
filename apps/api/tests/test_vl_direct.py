"""VL-direct 识别器：CSV → ExtractionDraft，逐行带来源与证据。

夹具用虚构料号。形态复刻实测：列错位、数值截断、明确不报价、副本、页码缺失、
表头中英混用。
"""
from __future__ import annotations

import pytest

from apps.api.intelligence.vl_direct import (
    build_draft,
    detect_rotations,
    map_columns,
    parse_csv,
)

HEAD = "row_type,材料名称,规格型号,单位,数量,单价,合价,copy_no,page"


def _csv(*rows: str) -> str:
    return "\n".join([HEAD, *rows])


# ─── 列名映射 ────────────────────────────────────────────────────────────────

def test_maps_document_own_headers():
    """提示词有意让模型用文档自己的表头（泛化要的），消费方必须自己映射。"""
    m = map_columns(["row_type", "材料（设备）名称", "规格型号", "计量单位",
                     "数量", "单价", "合价", "备注"])
    assert m["name"] == "材料（设备）名称" and m["spec"] == "规格型号"
    assert m["qty"] == "数量" and m["unit_price"] == "单价" and m["total_price"] == "合价"


def test_maps_english_headers():
    """同一个模型在不同文档上会自发切换表头语言，只认中文会把正确产物判成 0 行。"""
    m = map_columns(["row_type", "name", "spec", "unit", "quantity",
                     "unit_price", "total_price"])
    assert m["qty"] == "quantity" and m["total_price"] == "total_price"


def test_never_maps_excl_tax_column_to_incl_slot():
    """含税/不含税分列时选错就是把税前税后混为一谈，偏差恰好等于税率。"""
    m = map_columns(["单价（元）不含税", "合计（元）不含税", "税率", "价税合计（元）"])
    assert m["total_price"] == "价税合计（元）"
    assert m.get("unit_price") != "单价（元）不含税"
    assert m["unit_price_excl_tax"] == "单价（元）不含税"


# ─── 解析与来源 ──────────────────────────────────────────────────────────────

def test_rows_carry_page_and_row_indices():
    """行位证据是顺序直连与定向重读的输入，缺了只能退回载入顺序。"""
    d = build_draft(_csv("detail,电缆,A-1,米,10,5,50,1,1",
                         "detail,电缆,A-2,米,4,2,8,1,1",
                         "detail,电缆,A-3,米,2,3,6,1,2"),
                    file_path="x.pdf", page_count=2, processed_pages=[1, 2])
    idx = [(r.source_ref.page, r.fields["page_row_index"], r.fields["document_row_index"])
           for r in d.rows]
    assert idx == [(1, 1, 1), (1, 2, 2), (2, 1, 3)]


def test_row_type_normalised_and_unknown_kept_as_detail():
    """认不出的标签一律当明细——静默丢弃会把召回凭空做高。"""
    d = build_draft(_csv("detail,电缆,A-1,米,1,1,1,1,1",
                         "subtotal,小计,,,,,1,1,1",
                         "total,合计,,,,,1,1,1",
                         "весьма,电缆,A-2,米,1,1,1,1,1"),
                    file_path="x.pdf", page_count=1, processed_pages=[1])
    assert [r.row_type for r in d.rows] == [
        "quote_line", "subtotal", "grand_total", "quote_line"]


def test_not_quoted_marker_survives_parsing():
    """「/」与空白必须分开：前者合法、后者是缺陷。_num 会把两者都变成 None，
    所以判定必须发生在原始文本还在的时候。"""
    d = build_draft(_csv("detail,电缆,A-1,米,2,3,/,1,1",
                         "detail,电缆,A-2,米,2,3,,1,1"),
                    file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.rows[0].fields["not_quoted"] is True
    assert d.rows[1].fields["not_quoted"] is False
    assert d.rows[0].fields["total_price"] is None


def test_column_shift_is_flagged_per_row():
    """数据列数多于表头 = 整行按列名取值错位。DictReader 会把证据吃掉，故用 reader。"""
    text = _csv("detail,电缆,A-1,米,1,1,1,1,1",
                "detail,电缆,A,2,米,1,1,1,1,1")      # 规格被拆成两格
    d = build_draft(text, file_path="x.pdf", page_count=1, processed_pages=[1])
    assert "column_shift" not in d.rows[0].validation_flags
    assert "column_shift" in d.rows[1].validation_flags


def test_raw_cells_and_extra_columns_preserved():
    """原始值不得丢：没映射到槽位的列进 extra_fields，供下游和人工回溯。"""
    text = ("row_type,名称,规格,数量,单价,合价,交货期,page\n"
            "detail,电缆,A-1,1,2,2,30天,1")
    d = build_draft(text, file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.rows[0].raw_cells["交货期"] == "30天"
    assert d.rows[0].extra_fields.get("交货期") == "30天"


def test_missing_page_is_inferred_and_flagged_not_guessed_silently():
    """页码缺失时按前后文补，但必须留标记——补出来的不是事实。"""
    d = build_draft(_csv("detail,电缆,A-1,米,1,1,1,1,2",
                         "detail,电缆,A-2,米,1,1,1,1,"),
                    file_path="x.pdf", page_count=3, processed_pages=[1, 2, 3])
    assert d.rows[1].source_ref.page == 2
    assert "page_inferred" in d.rows[1].validation_flags


def test_row_ledger_conserves_rows():
    d = build_draft(_csv(*[f"detail,电缆,A-{i},米,1,1,1,1,1" for i in range(5)]),
                    file_path="x.pdf", page_count=1, processed_pages=[1])
    led = d.ledger.to_dict()
    assert led["recognized_rows"] == 5 and led["dropped_rows"] == 0


def test_empty_output_does_not_crash_and_reports_reason():
    """模型返回空必须变成"零行 + 原因"，不能变成异常或静默的空 draft。"""
    d = build_draft("", file_path="x.pdf", page_count=2, processed_pages=[1, 2])
    assert d.rows == []
    assert d.meta["diagnostics"]["reason"] == "empty_or_header_only"


def test_unresolved_orientation_forces_review():
    """方向没定下来的页可能整页读不出，而我们并不知道读没读出来——必须 REVIEW。"""
    d = build_draft(_csv("detail,电缆,A-1,米,1,1,1,1,1"),
                    file_path="x.pdf", page_count=2, processed_pages=[1, 2],
                    unresolved_pages=[2])
    assert d.quality.status == "REVIEW"
    assert any("orientation_unresolved_pages" in h for h in d.quality.blocking_reasons)
    assert d.meta["orientation_unresolved"] == [2]


def test_recognizer_marked_on_draft():
    d = build_draft(_csv("detail,电缆,A-1,米,1,1,1,1,1"),
                    file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.meta["recognizer"] == "vl_direct"
    assert d.rows[0].fields["parser_mode"] == "vl_direct"


# ─── 方向预检 ────────────────────────────────────────────────────────────────

def _img(_n):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return buf.getvalue()


def test_rotation_majority_wins():
    calls = []

    def orient(parts, prompt):
        calls.append(len(parts))
        return "1,90\n2,0"

    rot, unresolved = detect_rotations({1: _img(1), 2: _img(2)}, orient, votes=3)
    assert rot == {1: 90} and unresolved == []
    assert calls == [8, 8, 8], "每页 4 个旋转版本 × 2 页 = 8 张，投 3 轮"


def test_no_consensus_is_not_treated_as_no_rotation():
    """「没共识」≠「不用转」。合并两者会把检测失败当成"全都不用转"——
    实测缓存过一次这样的坏结论，两份文档金额差从 ±0.00 变成 −71 万和 −20 万。"""
    seq = iter(["1,90", "1,180", "1,270"])

    def orient(parts, prompt):
        return next(seq)

    rot, unresolved = detect_rotations({1: _img(1)}, orient, votes=3)
    assert rot == {} and unresolved == [1]


def _big_img(w=2400, h=1700):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def test_orientation_probes_are_downscaled_not_full_res():
    """方向预检的载荷是抽取的 12 倍（页 × 4 旋转 × 3 轮）。送全分辨率会让它
    占掉整条链路的绝大部分时间——实测 19 页要发 638MB，而抽取只要 53MB。
    离线验证基线用的就是缩略图（scale=0.30），这条必须保持。"""
    from PIL import Image
    import io as _io
    from apps.api.intelligence.vl_direct import ORIENT_PROBE_MAX_EDGE_PX

    sizes = []

    def orient(parts, prompt):
        for _label, b in parts:
            with Image.open(_io.BytesIO(b)) as im:
                sizes.append(max(im.size))
        return "1,0"

    detect_rotations({1: _big_img()}, orient, votes=1)
    assert sizes, "没有送出任何探测图"
    assert max(sizes) <= ORIENT_PROBE_MAX_EDGE_PX, (
        f"探测图长边 {max(sizes)}px 超过 {ORIENT_PROBE_MAX_EDGE_PX}px——"
        "全分辨率送方向预检会让链路慢一个数量级")


def test_full_resolution_is_still_used_for_extraction():
    """缩略图只能用于方向判断。抽取必须拿全分辨率，否则认不出字。"""
    from PIL import Image
    import io as _io
    from apps.api.intelligence.vl_direct import recognize_quote_vl
    import apps.api.intelligence.vl_direct as vd

    big = _big_img()
    seen = {}

    def fake_render(_path, pages):
        return {p: big for p in pages}

    def vl(imgs, prompt):
        with Image.open(_io.BytesIO(imgs[0])) as im:
            seen["extract_edge"] = max(im.size)
        return HEAD + "\ndetail,电缆,A-1,米,1,1,1,1,1"

    def orient(parts, prompt):
        with Image.open(_io.BytesIO(parts[0][1])) as im:
            seen["probe_edge"] = max(im.size)
        return "1,0"

    class _L:
        @staticmethod
        def get_page_count(_p):
            return 1
        render_pages = staticmethod(fake_render)

    orig = vd.DocumentLoader
    vd.DocumentLoader = _L
    try:
        recognize_quote_vl("x.pdf", vl_call=vl, orient_call=orient, votes=1)
    finally:
        vd.DocumentLoader = orig

    assert seen["probe_edge"] <= vd.ORIENT_PROBE_MAX_EDGE_PX
    assert seen["extract_edge"] == 2400, "抽取被降分辨率了"


def test_probe_downscale_computed_once_per_page():
    """3 轮投票复用同一份缩略图；每轮重缩等于把成本也乘以轮数。"""
    from PIL import Image
    calls = {"n": 0}
    real = Image.Image.resize

    def counting(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    Image.Image.resize = counting
    try:
        detect_rotations({1: _big_img(), 2: _big_img()},
                         lambda parts, prompt: "1,0\n2,0", votes=3)
    finally:
        Image.Image.resize = real
    assert calls["n"] == 2, f"2 页应只缩 2 次，实际 {calls['n']} 次"


def test_orientation_probe_failure_is_survivable():
    def boom(parts, prompt):
        raise RuntimeError("probe down")

    rot, unresolved = detect_rotations({1: _img(1)}, boom, votes=2)
    assert rot == {} and unresolved == [1]

# ─── 表头语言对称性与税额隔离（2026-08-10 语言 A/B）────────────────────────────
#
# 语言 A/B 实测：英文提示词会让模型**翻译**文档表头，且三次翻译各不相同
# （spec_model / specification / spec_model）。中文提示词下模型也曾自发输出英文表头。
# 因此表头语言不受控——中英模式必须对称，否则换个语言就静默丢税基。

_TAX_CN = ["row_type", "项目名称", "规格", "单位", "数量",
           "单价(元)不含税", "合计(元)不含税", "税率", "税额(元)", "价税合计(元)"]
_TAX_EN = ["row_type", "item_name", "spec", "unit", "quantity",
           "unit_price_excl_tax", "total_excl_tax", "tax_rate", "tax_amount", "total_incl_tax"]


@pytest.mark.parametrize("headers,incl,excl", [
    (_TAX_CN, "价税合计(元)", "合计(元)不含税"),
    (_TAX_EN, "total_incl_tax", "total_excl_tax"),
    (["total_with_tax", "amount_excl_tax", "tax_amount"], "total_with_tax", "amount_excl_tax"),
])
def test_tax_basis_survives_either_header_language(headers, incl, excl):
    m = map_columns(headers)
    assert m["total_price"] == incl
    assert m["total_price_excl_tax"] == excl


@pytest.mark.parametrize("headers", [_TAX_CN, _TAX_EN,
                                     ["name", "qty", "tax_amount"],
                                     ["名称", "数量", "税额(元)"]])
def test_tax_amount_never_lands_in_a_price_slot(headers):
    """税额落进合计槽位下游**察觉不到**：税额 ≈ 不含税合价 × 税率，本身自洽，
    逐行算术校验照样通过，只有整份金额偏小。曾由末档模式 ("amount",) 命中。"""
    m = map_columns(headers)
    for slot in ("unit_price", "total_price",
                 "unit_price_excl_tax", "total_price_excl_tax"):
        got = (m.get(slot) or "").lower()
        assert "税额" not in got and "tax_amount" not in got, f"{slot} 取到了税额列"


def test_excl_tax_column_is_never_silently_dropped():
    """不含税列若一个槽位都进不去，它的值就凭空消失了——比映射错更难发现。"""
    m = map_columns(_TAX_EN)
    assert m.get("unit_price_excl_tax") == "unit_price_excl_tax"
    assert m.get("unit_price") is None, "不含税单价不得占用含税槽位"


def test_unmappable_price_column_blocks_instead_of_reporting_zero_money():
    """模式列表永远补不完，所以要有不依赖它的兜底。

    实测一次真实失败：模型把表头译成 unit_price_ex_tax / total_inc_tax，
    total_price 一个槽位都没匹配上，89 行合价全空、金额短 824,915 元（88.5%），
    而结构门判 ok、逐行算术无异常——错误完全静默。
    「读到了行却读不到钱」符合 BLOCKED 的「无有效报价」。
    """
    text = ("row_type,item,spec,unit,qty,prix_unitaire,montant_total,page\n"
            "detail,cable,A-1,m,10,5.5,55.0,1\n"
            "detail,cable,A-2,m,4,2.0,8.0,1")
    d = build_draft(text, file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.quality.status == "BLOCKED"
    assert any("no_price_column_mapped" in r for r in d.quality.blocking_reasons)
    # 丢掉的那笔钱要指名道姓，否则人工无从下手
    assert "montant_total" in str(d.meta["diagnostics"]["unmapped_numeric_columns"])


def test_recognised_price_column_does_not_trigger_the_block():
    d = build_draft(HEAD + "\ndetail,电缆,A-1,米,10,5,50,1,1",
                    file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.quality.status != "BLOCKED"
    assert d.meta["diagnostics"]["has_price_column"] is True


def test_excl_tax_only_document_still_counts_as_having_price():
    """只有不含税合价也是有价——不能因为没有含税列就判无有效报价。"""
    text = ("row_type,名称,规格,单位,数量,单价不含税,合计不含税,税率,page\n"
            "detail,电缆,A-1,米,10,5,50,0.13,1")
    d = build_draft(text, file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.meta["diagnostics"]["has_price_column"] is True
    assert d.quality.status != "BLOCKED"


def test_unit_price_alone_is_not_enough():
    """只有单价没有合价时，合价只能由 数量×单价 推出——那正是被禁的静默派生。"""
    text = ("row_type,名称,规格,单位,数量,单价,page\n"
            "detail,电缆,A-1,米,10,5,1")
    d = build_draft(text, file_path="x.pdf", page_count=1, processed_pages=[1])
    assert d.meta["diagnostics"]["has_price_column"] is False
    assert d.quality.status == "BLOCKED"
