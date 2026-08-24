"""空格子补位（docs/design/33）—— 四个绑定条件逐条锁住。

**全程离线。** 第二个模型一律用桩函数，桩返回什么由测试指定，因此"方向错时模型
返回格式完整的错值"这种最危险的情况能被**正面构造**出来，而不是碰运气碰不到。

夹具用真实快照 + 真实标答（泰科龙第 10 页那 9 行，实测占该文档合价缺口的 100%），
不手搓：这道门要面对的是真实的数值分布和真实的错位形态。
"""
from __future__ import annotations

import csv
import io

import pytest

from apps.api.intelligence import gap_fill
from apps.api.intelligence.extraction_draft import DraftRow, SourceRef

SLUG = "quote_taikelong"


# ── 造行的小工具（不依赖快照的纯逻辑测试用） ──────────────────────────────

def _row(idx: int, page: int, **fields) -> DraftRow:
    return DraftRow(row_index=idx, row_type="quote_line", raw_cells={},
                    fields=dict(fields), source_ref=SourceRef(page=page))


def _real_png() -> bytes:
    """真的能被 PIL 打开的图。

    早一版这里用 `b"X"` 当占位——旋转分支一跑 PIL 就抛，异常被"调用失败"那个
    except 吞掉，于是 90°/270° 静默跳过，测试红了才发现。顺带查出生产代码把
    **旋转失败**和**模型拒答**归进了同一个 except：前者是本地缺陷、后者是预期
    内的正常结果（红章触发安全审查），混在一起会让真缺陷永远伪装成"模型不给力"。
    两边都修了；这个夹具保证旋转分支真的被走到。
    """
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


# ── ① 默认关闭 ──────────────────────────────────────────────────────────────

def test_no_filler_is_a_noop():
    """`filler=None` 时一个字段都不该动——这是它敢默认接进生产的前提，
    也是 7 份快照回放指标能保持逐字节不变的原因。"""
    rows = [_row(0, 1, seq="1", qty="2", unit_price="10", total_price=""),
            _row(1, 1, seq="2", qty="3", unit_price="10", total_price="30")]
    before = [dict(r.fields) for r in rows]
    rep = gap_fill.fill_gaps(rows, filler=None, render_page=lambda p: _real_png())
    assert rep.fields_filled == 0
    assert [dict(r.fields) for r in rows] == before


# ── ② 找洞的判据 ────────────────────────────────────────────────────────────

def test_finds_only_empty_cells_in_columns_the_table_has():
    """判据是 design/33 §4.1 唯一允许的那条：**这张表有这个列，而这一格什么都没读到**。"""
    rows = [_row(0, 1, seq="1", qty="2", unit_price="10", total_price="20"),
            _row(1, 1, seq="2", qty="3", unit_price="10", total_price="")]
    assert gap_fill.find_gaps(rows) == {1: [1]}


def test_a_column_the_table_does_not_have_is_not_a_gap():
    """整页都没有税率列 ≠ 每一行的税率都是洞。无税版式（绵存、徐汇四家）
    真实存在，把它当成洞会让补位对着一个不存在的列发问。"""
    rows = [_row(0, 1, seq="1", qty="2", unit_price="10", total_price="20"),
            _row(1, 1, seq="2", qty="3", unit_price="10", total_price="30")]
    assert gap_fill.find_gaps(rows) == {}


def test_explicit_not_quoted_is_not_a_gap():
    """格子里印着 `/` 是**投标方明确不报此项**，合法事实，不是缺陷。

    CLAUDE.md 明令"原文明确不报价"与"读不到"不得合并成同一个空值语义；补它
    等于逼用户编一个金额出来，正是这套系统最该防的东西。
    """
    rows = [_row(0, 1, seq="1", qty="2", unit_price="10", total_price="20"),
            _row(1, 1, seq="2", qty="3", unit_price="10", total_price="/")]
    assert gap_fill.find_gaps(rows) == {}


# ── ③ 写回的四个条件 ────────────────────────────────────────────────────────

def _filler(csv_text: str):
    return lambda _png: csv_text


def _one_page_rows():
    return [_row(0, 1, seq="1", qty="2", unit_price="10.00", total_price="20.00"),
            _row(1, 1, seq="2", qty="", unit_price="", total_price="")]


def test_fills_and_marks_provenance():
    """条件②：逐字段标 `field_sources="llm"`，不冒充直读；行上留标记进疑点收件箱。"""
    rows = _one_page_rows()
    rep = gap_fill.fill_gaps(
        rows, filler=_filler("seq,qty,unit_price,total_price\n2,3,10.00,30.00\n"),
        render_page=lambda p: _real_png(), angles=(0,))
    assert rep.rows_filled == 1 and rep.fields_filled == 3
    assert rows[1].fields["total_price"] == "30.00"
    assert rows[1].field_sources["total_price"] == gap_fill.FILL_SOURCE
    assert gap_fill.FILL_FLAG in rows[1].validation_flags
    # 没补过的行不该被打上标记
    assert gap_fill.FILL_FLAG not in rows[0].validation_flags


def test_never_overwrites_an_existing_value():
    """条件①：只补空格子。覆盖已识别值是 CLAUDE.md §4 明禁的另一回事——
    模型即便回了一个不同的数，原值也必须原封不动。"""
    rows = _one_page_rows()
    gap_fill.fill_gaps(
        rows,
        filler=_filler("seq,qty,unit_price,total_price\n1,999,999,999\n2,3,10.00,30.00\n"),
        render_page=lambda p: _real_png(), angles=(0,))
    assert rows[0].fields == {"seq": "1", "qty": "2", "unit_price": "10.00",
                              "total_price": "20.00"}
    assert rows[0].field_sources == {}


def test_row_that_fails_arithmetic_is_discarded_whole():
    """条件③：过不了恒等式**整行丢弃**，不是逐字段挑能过的留下。

    挑剩的组合会让一行由"直读的一半 + 拼上去的一半"构成，那个组合谁也没验证过。
    留空是诚实状态，自洽不了的数字不是。
    """
    rows = _one_page_rows()
    # 3 × 10 = 30，这里给 999 —— 自相矛盾
    rep = gap_fill.fill_gaps(
        rows, filler=_filler("seq,qty,unit_price,total_price\n2,3,10.00,999.00\n"),
        render_page=lambda p: _real_png(), angles=(0,))
    assert rep.fields_filled == 0
    assert rows[1].fields["qty"] == "" and rows[1].fields["total_price"] == ""
    assert sum(o.rejected_by_gate for o in rep.outcomes) == 1


def test_non_numeric_answer_is_dropped():
    rows = _one_page_rows()
    rep = gap_fill.fill_gaps(
        rows, filler=_filler("seq,qty,unit_price,total_price\n2,面议,面议,面议\n"),
        render_page=lambda p: _real_png(), angles=(0,))
    assert rep.fields_filled == 0


# ── ④ 方向惰性扇出 ──────────────────────────────────────────────────────────

def test_stops_at_the_first_orientation_that_passes():
    """§6 决策 1：惰性扇出——按顺序试，谁先过门就停，不是每次都发三次。"""
    rows = _one_page_rows()
    seen: list[int] = []

    def _f(png: bytes) -> str:
        # 用旋转后字节长度区分方向不可靠，改用调用次数：第 1 次(0°)给错值，
        # 第 2 次(90°)给对值，验证它停在 90° 不再试 270°。
        seen.append(len(seen))
        return ("seq,qty,unit_price,total_price\n2,3,10.00,999.00\n" if len(seen) == 1
                else "seq,qty,unit_price,total_price\n2,3,10.00,30.00\n")

    rep = gap_fill.fill_gaps(rows, filler=_f, render_page=lambda p: _real_png(),
                             angles=(0, 90, 270))
    assert len(seen) == 2, "过门之后不该继续试剩下的方向"
    assert rep.outcomes[0].angle_used == 90
    assert rep.outcomes[0].angles_tried == [0, 90]


def test_when_no_orientation_passes_nothing_is_filled_and_it_says_so():
    rows = _one_page_rows()
    rep = gap_fill.fill_gaps(
        rows, filler=_filler("seq,qty,unit_price,total_price\n2,3,10.00,999.00\n"),
        render_page=lambda p: _real_png(), angles=(0, 90, 270))
    assert rep.fields_filled == 0
    assert rep.outcomes[0].angle_used is None
    assert "保持留空" in rep.outcomes[0].error


def test_a_refusing_orientation_is_not_an_error():
    """0° 被安全审查拒绝（DataInspectionFailed，多半是红章）是**正常结果**，
    不是错误——必须继续试下一个方向（design/33 §2.3）。"""
    rows = _one_page_rows()
    calls = {"n": 0}

    def _f(png: bytes) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DataInspectionFailed")
        return "seq,qty,unit_price,total_price\n2,3,10.00,30.00\n"

    rep = gap_fill.fill_gaps(rows, filler=_f, render_page=lambda p: _real_png(),
                             angles=(0, 90))
    assert rep.rows_filled == 1
    assert rep.outcomes[0].angle_used == 90


def test_render_failure_is_reported_not_raised():
    rows = _one_page_rows()
    rep = gap_fill.fill_gaps(rows, filler=_filler("seq,qty\n2,3\n"),
                             render_page=lambda p: None)
    assert rep.fields_filled == 0
    assert rep.outcomes[0].error


# ── ⑤ 真实语料：泰科龙第 10 页 ──────────────────────────────────────────────

def _real_rows():
    try:
        from apps.api.tests.test_scenarios_e2e import SNAPS, recognize_snapshot
    except Exception:                                              # pragma: no cover
        pytest.skip("场景 E2E 夹具不可用")
    if not (SNAPS / f"{SLUG}.json").exists():
        pytest.skip(f"快照缺失：{SLUG}.json")
    return recognize_snapshot(SLUG)


def _golden():
    from apps.api.tests.test_scenarios_e2e import DOCS, SNAPSHOT_REFERENCE, read_reference
    return read_reference(DOCS / SNAPSHOT_REFERENCE[SLUG])


def _answer_csv(rows, ref, targets, *, shifted: bool) -> str:
    """用标答造模型回答。`shifted=True` 复现 design/33 §2.3 实测的 270° 形态：
    把**税额**填进价税合计——格式完整、看着合理、就是错的。"""
    from apps.api.tests.test_scenarios_e2e import parse_num
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "qty", "unit_price_excl_tax", "total_price_excl_tax",
                "tax_rate", "tax_amount", "unit_price_incl_tax", "total_price_incl_tax"])
    for i in targets:
        g = ref[i]
        tp = parse_num(g.get("total_price"))
        tax = round(float(tp) * 0.13, 2) if tp else ""
        incl = tax if shifted else (round(float(tp) * 1.13, 2) if tp else "")
        w.writerow([str(rows[i].fields.get("seq") or "").strip(), g.get("qty"),
                    g.get("unit_price"), g.get("total_price"), "0.13", tax, "", incl])
    return buf.getvalue()


def test_real_gap_is_recovered_and_the_total_closes():
    """泰科龙第 10 页那 9 行 = 该文档 -26.22% 合价缺口的 **100%**
    （89 行对 89 条逐位比对得出）。补上之后合计应当归零。"""
    from apps.api.tests.test_scenarios_e2e import _total_of, parse_num

    rows, ref = _real_rows(), _golden()
    targets = [i for v in gap_fill.find_gaps(rows).values() for i in v]
    assert targets, "这份快照里应当存在空格子"

    rep = gap_fill.fill_gaps(
        rows, filler=_filler(_answer_csv(rows, ref, targets, shifted=False)),
        render_page=lambda p: _real_png(), angles=(0,))
    assert rep.rows_filled >= 9

    exp = sum(parse_num(r["total_price"]) for r in ref if not r["not_quoted"])
    got = sum(_total_of(r.fields) or 0 for r in rows)
    assert abs((got - exp) / exp) < 0.001, f"补位后合价仍差 {(got-exp)/exp*100:.2f}%"


def test_a_shifted_answer_never_reaches_the_money():
    """**这条是整个特性敢存在的理由。**

    方向错的时候模型不会响亮地失败，它返回一个格式完整、貌似合理的错值。
    实测（design/33 §2.4）恒等式对两个方向给出 9/9 与 0/9 的完美分离——
    这里把 0/9 那一半钉死：错位答案落地之后，合价合计必须**一分不动**。
    """
    from apps.api.tests.test_scenarios_e2e import _total_of

    rows, ref = _real_rows(), _golden()
    targets = [i for v in gap_fill.find_gaps(rows).values() for i in v]
    before = sum(_total_of(r.fields) or 0 for r in rows)

    rep = gap_fill.fill_gaps(
        rows, filler=_filler(_answer_csv(rows, ref, targets, shifted=True)),
        render_page=lambda p: _real_png(), angles=(0,))

    after = sum(_total_of(r.fields) or 0 for r in rows)
    assert after == before, "错位答案把钱写进来了——算术门没拦住"
    page10 = [o for o in rep.outcomes if o.page == 10]
    assert page10 and page10[0].rows_filled == 0
    assert page10[0].rejected_by_gate == 9
