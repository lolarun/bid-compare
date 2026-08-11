"""入库前结构门：列错位 + 重复行。

夹具用虚构料号，形态复刻 2026-08-09 七份实测里观察到的三种真实故障：
右移（表头少一列）、左移（首列被类目名占掉）、整批页重复抽取。
"""
from __future__ import annotations

import pytest

from apps.api.services.draft_integrity import (
    ARITHMETIC_FLAG,
    BLOCKED,
    COLUMN_SHIFT_FLAG,
    DUPLICATE_FLAG,
    OK,
    REVIEW,
    TRUNCATION_FLAG,
    annotate_items,
    arithmetic_deviation,
    check_arithmetic,
    check_column_alignment,
    check_row_arithmetic,
    check_table_integrity,
    corroborate_truncation,
    detect_truncated_numbers,
    find_duplicate_rows,
    read_table_rows,
)

HEADER = ["序号", "名称", "规格型号", "单位", "数量", "单价", "合价", "备注"]


def _row(seq, spec, qty, price, total, tail="备注A"):
    return [str(seq), "示例线缆", spec, "米", str(qty), str(price), str(total), tail]


# ─── ① 列错位 ────────────────────────────────────────────────────────────────

def test_aligned_table_passes():
    rows = [_row(i, f"AA-{i}*10", 10, 2, 20) for i in range(1, 6)]
    r = check_column_alignment(HEADER, rows)
    assert r.verdict == OK
    assert r.bad_rows == []


def test_right_shift_blocks_whole_table():
    """表头把「规格/型号」并成一列、数据仍是两列 → 每行多一个单元格。"""
    rows = [[str(i), "示例线缆", "AA", f"{i}*10", "米", "10", "2", "20", "备注A"]
            for i in range(1, 6)]
    r = check_column_alignment(HEADER, rows)
    assert r.verdict == BLOCKED, "大面积右移必须整份阻断，不能只标注"
    assert len(r.extra_rows) == 5
    assert all(x.kind == "extra_cells" for x in r.extra_rows)


def test_single_extra_cell_row_is_review_not_blocked():
    """一行异常不该牵连整份——但该行本身仍进不了库。"""
    rows = [_row(i, f"AA-{i}*10", 10, 2, 20) for i in range(1, 300)]
    rows[7] = rows[7] + ["多出来的一格"]
    r = check_column_alignment(HEADER, rows)
    assert r.verdict == REVIEW
    assert r.bad_row_indices == {7}


def test_missing_cells_is_review():
    rows = [_row(i, f"AA-{i}*10", 10, 2, 20) for i in range(1, 4)]
    rows[1] = rows[1][:5]
    r = check_column_alignment(HEADER, rows)
    assert r.verdict == REVIEW
    assert r.missing_rows[0].kind == "missing_cells"


def test_trailing_empty_cells_are_not_a_defect():
    """CSV 写出常给尾列补空串；那不是错位，不能报缺陷。"""
    rows = [_row(i, f"AA-{i}*10", 10, 2, 20) + [""] for i in range(1, 4)]
    assert check_column_alignment(HEADER, rows).verdict == OK


def test_empty_header_blocks():
    assert check_column_alignment([], [["a"]]).verdict == BLOCKED


# ─── ② 重复行 ────────────────────────────────────────────────────────────────

def _item(spec, qty, price, total, name="示例线缆"):
    return {"material": name, "spec": spec, "qty": qty,
            "unit_price": price, "total_price": total}


def test_no_duplicates():
    items = [_item(f"AA-{i}*10", 10, 2 + i, 20 + i * 10) for i in range(5)]
    assert find_duplicate_rows(items).verdict == OK


def test_same_spec_different_price_is_not_duplicate():
    """同料同量不同价是合法的（分批/分楼层报价），不得判成重复。"""
    items = [_item("AA-1*10", 10, 2, 20), _item("AA-1*10", 10, 3, 30)]
    assert find_duplicate_rows(items).verdict == OK


def test_wholesale_duplication_blocks_and_keeps_first_row():
    """整批页被读两遍：金额虚增一倍 → BLOCKED，但第一份行不被牵连。"""
    base = [_item(f"AA-{i}*10", 10 + i, 2 + i, (10 + i) * (2 + i)) for i in range(20)]
    items = base + [dict(x) for x in base]
    rep = find_duplicate_rows(items)
    assert rep.verdict == BLOCKED
    assert len(rep.groups) == 20
    assert rep.duplicate_row_indices == set(range(20, 40)), "只有后一份算重复"
    assert rep.amount_ratio == pytest.approx(0.5, abs=1e-6)


def test_small_duplication_is_review_only():
    items = [_item(f"AA-{i}*10", 10, 2, 20) for i in range(40)]
    items.append(dict(items[0]))
    rep = find_duplicate_rows(items)
    assert rep.verdict == REVIEW, "少量重复可能是真实分行，交人工判"
    assert rep.duplicate_row_indices == {40}


def test_rows_without_name_and_spec_are_skipped():
    items = [_item("", 1, 1, 1, name=""), _item("", 1, 1, 1, name="")]
    assert find_duplicate_rows(items).verdict == OK


def test_duplicate_amount_falls_back_to_qty_times_price():
    """合价缺失时用 数量×单价 估重复规模——仅用于判定，绝不写回字段。"""
    items = [_item("AA-1*10", 10, 2, None), _item("AA-1*10", 10, 2, None)]
    rep = find_duplicate_rows(items)
    assert rep.duplicate_amount == pytest.approx(20.0)
    assert all(i["total_price"] is None for i in items), "原值不得被改写"


# ─── 合并入口与标注 ──────────────────────────────────────────────────────────

def test_annotate_items_appends_flags_without_touching_values():
    rows = [_row(1, "AA-1*10", 10, 2, 20), _row(2, "AA-2*10", 10, 2, 20) + ["x"],
            _row(1, "AA-1*10", 10, 2, 20)]
    items = [_item("AA-1*10", 10, 2, 20), _item("AA-2*10", 10, 2, 20),
             _item("AA-1*10", 10, 2, 20)]
    items[0]["validation_flags"] = ["existing"]
    rep = check_table_integrity(HEADER, rows, items)
    annotate_items(items, rep)
    assert items[0]["validation_flags"] == ["existing"], "正常行不加标记"
    assert COLUMN_SHIFT_FLAG in items[1]["validation_flags"]
    assert DUPLICATE_FLAG in items[2]["validation_flags"]
    assert items[2]["unit_price"] == 2, "标注不得改动任何原值"


# ─── ③ 算术闭合 ──────────────────────────────────────────────────────────────

def test_arithmetic_ok():
    r = check_row_arithmetic({"qty": 3, "unit_price": 10, "total_price": 30})
    assert r.status == "ok"


def test_arithmetic_mismatch():
    r = check_row_arithmetic({"qty": 3, "unit_price": 10, "total_price": 45})
    assert r.status == "mismatch"


def test_tax_bases_are_paired_never_crossed():
    """不含税单价 × 数量 只能对不含税合价。跨税基比会把每一行都判错，
    且偏差恰好等于税率——看起来像系统性错误，其实是比错了尺子。"""
    r = check_row_arithmetic({"qty": 1, "unit_price_excl_tax": 100,
                              "total_price_excl_tax": 100,
                              "unit_price_incl_tax": 113, "total_price_incl_tax": 113})
    assert r.status == "ok"
    assert r.basis == "unit_price_excl_tax|total_price_excl_tax"


def test_tax_basis_suspect_when_only_mixed_columns_exist():
    r = check_row_arithmetic({"qty": 1, "unit_price": 100, "total_price": 113})
    assert r.status == "tax_basis_suspect", "增值税量级的偏差不该报成算错"


def test_quote_multiplier_is_recorded_not_corrected():
    """按根/按束报价导致 合价=2×数量×单价 是报价口径的选择，不是错误。
    只能观测和标记，禁止据此修正原值或反推数量。"""
    r = check_row_arithmetic({"qty": 10, "unit_price": 100, "total_price": 2000})
    assert r.status == "multiplier"
    assert r.implied_multiplier == 2.0
    assert r.total_price == 2000, "原值不得被改写"


def test_missing_inputs_are_not_evaluable_not_pass():
    """三缺一记为不可评估——当成通过会把分母撑大，把真实错误稀释到阈值以下。"""
    for row in ({"qty": 3, "unit_price": 10}, {"qty": 0, "unit_price": 10, "total_price": 0},
                {}):
        assert check_row_arithmetic(row).status == "not_evaluable"
    rep = check_arithmetic([{"qty": 3, "unit_price": 10}] * 5)
    assert rep.evaluable == 0 and rep.verdict == OK


def test_arithmetic_rate_escalates_to_blocked():
    good = [{"qty": 1, "unit_price": 10, "total_price": 10} for _ in range(90)]
    bad = [{"qty": 1, "unit_price": 10, "total_price": 99} for _ in range(10)]
    assert check_arithmetic(good + bad).verdict == BLOCKED
    assert check_arithmetic(good + bad[:2]).verdict == REVIEW


def test_zero_total_does_not_explode_deviation():
    """分母取 max(|合价|, |数量×单价|)：只用合价当分母时，合价被读成极小值
    会得到天文数字的偏差率。"""
    assert 0.0 <= arithmetic_deviation(1, 100, 0.01) <= 1.0


# ─── ④ 截断检测（按列自校准，不得依赖固定宽度/列名）──────────────────────────

def _col(values, name="合价"):
    return [name], [[v] for v in values]


def test_truncation_detected_by_column_self_calibration():
    """判据全部来自该列自身：宽度上限处堆积 + 那一片的小数位少于本列常见位数。"""
    normal = [f"{i}.{i % 10}{(i + 3) % 10}" for i in range(1, 41)]      # 2 位小数，较短
    capped = ["1956390.", "1143959.", "1234567.", "2345678."]           # 卡在 8 字符、丢小数
    header, rows = _col(normal + capped)
    rep = detect_truncated_numbers(header, rows)
    assert rep.verdict == REVIEW
    assert len(rep.suspects) == 4
    assert rep.columns == ["合价"]


def test_no_truncation_flag_on_naturally_wide_values():
    """正常文档里数值宽度连续分布，最宽的几个照样有完整小数 → 不得报。"""
    vals = [f"{i * 137}.{i % 100:02d}" for i in range(1, 60)]
    assert detect_truncated_numbers(*_col(vals)).verdict == OK


def test_integer_column_never_flags():
    """整数列的常见小数位是 0，没有值能比 0 位更少——不该有任何命中。"""
    vals = [str(i * 7) for i in range(1, 60)] + ["99999999"] * 10
    assert detect_truncated_numbers(*_col(vals)).suspects == []


def test_small_sample_is_not_judged():
    """样本不足不下结论，避免小表瞎报。"""
    vals = ["1.23", "2.34", "12345678."]
    assert detect_truncated_numbers(*_col(vals)).verdict == OK


def test_uniform_width_column_has_no_baseline():
    """全列同宽 → 无基线可比 → 不下结论（而不是全部判成截断）。"""
    vals = ["1234567." for _ in range(40)]
    assert detect_truncated_numbers(*_col(vals)).suspects == []


def test_truncation_is_column_local_not_global():
    """一列被截断不能牵连另一列；每列各自校准。"""
    header = ["数量", "合价"]
    rows = [[f"{i}.{i % 10}{i % 7}", f"{i}.{i % 10}{i % 7}"] for i in range(1, 41)]
    rows += [["9.99", "1956390."], ["8.88", "1143959."], ["7.77", "1234567."]]
    rep = detect_truncated_numbers(header, rows)
    assert rep.columns == ["合价"]


def test_corroboration_uses_arithmetic_residual():
    """被截断的合价，数量×单价 − 合价 必然是正的且小于一个计价单位。"""
    header = ["合价"]
    normal = [[f"{i}.{i % 10}{(i + 1) % 10}"] for i in range(1, 41)]
    rows = normal + [["1956390."], ["1143959."], ["1234567."]]
    items = [{"qty": 1, "unit_price": 1, "total_price": r[0]} for r in normal]
    items += [{"qty": 1, "unit_price": 1956390.45, "total_price": 1956390.0},
              {"qty": 1, "unit_price": 1143959.6, "total_price": 1143959.0},
              {"qty": 1, "unit_price": 9999999.0, "total_price": 1234567.0}]
    rep = corroborate_truncation(detect_truncated_numbers(header, rows), items)
    got = {s.value: s.corroborated for s in rep.suspects}
    assert got["1956390."] is True and got["1143959."] is True
    assert got["1234567."] is False, "残差远大于一个计价单位 → 不是截断"


def test_sparse_truncation_still_detected_pileup_is_only_confidence():
    """小表里只有三两个值足够长时也必须能发现。
    宽度堆积只作为置信度记录——拿它当门槛会让检测取决于"恰好有多少个值够长"。"""
    normal = [f"{i}.{i % 10}{(i + 3) % 10}" for i in range(1, 41)]   # 大量短值
    capped = ["1956390.", "1143959.", "1234567."]                    # 只有 3 个长值
    rep = detect_truncated_numbers(*_col(normal + capped))
    assert len(rep.suspects) == 3
    assert all(s.width_pileup is False for s in rep.suspects), "此处并未堆积，但仍应报出"


def test_currency_symbols_and_separators_do_not_break_width():
    vals = [f"¥{i}.{i % 10}{(i + 2) % 10}" for i in range(1, 41)] + ["1,956,390.", "1143959.",
                                                                     "1234567."]
    rep = detect_truncated_numbers(*_col(vals))
    assert len(rep.suspects) == 3, "千分位与货币符号应在归一后再比宽度"


class _FakeDb:
    def __init__(self):
        self.rolled_back = False

    def rollback(self):
        self.rolled_back = True


def test_gate_lets_legitimate_duplicates_through_with_a_flag():
    """同型号同量同价出现在两个系统里是正常清单——标注、放行，不得拒收。

    实测三份真实阀门文档各有 3~6 组这样的行，且逐行与 golden 完全一致；
    早期版本在这里 422，把 4 个已通过的集成测试打红。
    """
    from apps.api.services.quote_confirmation_service import _gate_integrity
    items = [_item(f"DN{20 + i}", 1, 10 + i, 10 + i, name="闸阀") for i in range(40)]
    items += [dict(items[0]), dict(items[1])]
    db = _FakeDb()
    out = _gate_integrity(db, items)
    assert db.rolled_back is False
    assert out["duplicate_verdict"] == REVIEW
    assert out["duplicate_rows"] == 2
    assert DUPLICATE_FLAG in items[40]["validation_flags"], "放行也必须留标记"
    assert items[0].get("validation_flags") is None, "第一份不被牵连"


def test_gate_blocks_column_shift_even_for_a_single_row():
    """列错位没有合法形态——一行也不放行。"""
    from fastapi import HTTPException
    from apps.api.services.quote_confirmation_service import _gate_integrity
    items = [_item(f"DN{20 + i}", 1, 10 + i, 10 + i) for i in range(10)]
    items[3]["validation_flags"] = [COLUMN_SHIFT_FLAG]
    db = _FakeDb()
    with pytest.raises(HTTPException) as ei:
        _gate_integrity(db, items)
    assert ei.value.status_code == 422
    assert ei.value.detail["error"] == "structural_integrity_requires_review"
    assert db.rolled_back is True


def test_gate_blocks_wholesale_duplication():
    from fastapi import HTTPException
    from apps.api.services.quote_confirmation_service import _gate_integrity
    base = [_item(f"DN{20 + i}", 1 + i, 10 + i, (1 + i) * (10 + i)) for i in range(20)]
    db = _FakeDb()
    with pytest.raises(HTTPException) as ei:
        _gate_integrity(db, base + [dict(x) for x in base])
    assert ei.value.detail["duplicates"]["verdict"] == BLOCKED
    assert db.rolled_back is True


def test_gate_honours_explicit_ack():
    """人工核对过原文即可放行——与派生金额门一致，系统不替用户做判断。"""
    from apps.api.services.quote_confirmation_service import _gate_integrity
    items = [_item(f"DN{20 + i}", 1, 10 + i, 10 + i) for i in range(10)]
    items[3]["validation_flags"] = [COLUMN_SHIFT_FLAG]
    items[3]["integrity_ack"] = True
    assert _gate_integrity(_FakeDb(), items)["column_shift_rows"] == 1


def test_read_table_rows_preserves_ragged_shape(tmp_path):
    """DictReader 会把多出的格塞进 restkey、缺的补 None，错位证据就没了。"""
    p = tmp_path / "q.csv"
    p.write_text("a,b,c\n1,2,3\n1,2,3,4\n1,2\n", encoding="utf-8")
    header, rows = read_table_rows(p)
    assert header == ["a", "b", "c"]
    assert [len(r) for r in rows] == [3, 4, 2]
    # 3 行里 1 行多格 → 占比远超阈值，按整份阻断（小表不该被比例稀释）
    assert check_column_alignment(header, rows).verdict == BLOCKED


# ─── 缺格行的整份升级（2026-08-10 上海浦东）────────────────────────────────────
#
# 实测：模型在 CSV 中途开始"出声思考"（写下疑问、切成竖线分隔、边写边推翻自己），
# 279 行里 38 行不再是 CSV。这些行格数不足，而当时 missing_cells 无论多少行都只
# 判 REVIEW，于是一份 13.6% 结构解析失败、金额短 124 万的文件以"人工复核"身份
# 放行。BLOCKED 的定义是"无可靠结构"——13.6% 解析失败正是它。

def _rows(full: int, short: int, wide: int = 0) -> list[list]:
    return ([["a", "b", "c"]] * full + [["a"]] * short
            + [["a", "b", "c", "d", "e"]] * wide)


def test_widespread_missing_cells_blocks_the_document():
    r = check_column_alignment(["a", "b", "c"], _rows(241, 38))    # 13.6%
    assert r.verdict == "blocked"


def test_isolated_short_row_stays_review():
    """注释行、只有两个字段的小计行本来就短——个别短行不能牵连整份。"""
    r = check_column_alignment(["a", "b", "c"], _rows(89, 1))      # 1.1%
    assert r.verdict == "review"


def test_missing_cell_escalation_is_ratio_only_not_absolute_count():
    """合法短行的数量随文档规模增长（每个分部一行小计）；固定行数阈值会在长
    文档上误报。10 行短行在 1000 行文档里是 1%，不该阻断。"""
    r = check_column_alignment(["a", "b", "c"], _rows(990, 10))
    assert r.verdict == "review"


def test_extra_and_missing_escalate_independently_worst_wins():
    """两类性质不同，各自判各自的，取较严者——否则一类会掩盖另一类。"""
    r = check_column_alignment(["a", "b", "c"], _rows(200, 1, wide=10))
    assert r.verdict == "blocked", "大量多格行必须阻断，不因缺格行只有 1 行而降级"


def test_clean_table_is_ok():
    assert check_column_alignment(["a", "b", "c"], _rows(90, 0)).verdict == "ok"


# ─── 序号连续性：行数守恒的独立判据（docs/design/21 §2.1）────────────────────
#
# VL 路径的行数台账是同义反复——expected 与 extracted 同源，结构上报不出丢行。
# 序号是文档自己印在纸上的，不由抽取质量决定，是目前唯一的独立判据。

from apps.api.services.draft_integrity import check_sequence_continuity  # noqa: E402


def _seq_items(seqs):
    return [{"seq": str(s)} for s in seqs]


def test_complete_sequence_is_ok():
    assert check_sequence_continuity(_seq_items(range(1, 137))).verdict == "ok"


def test_single_gap_localises_the_missing_row():
    """比"少了一行"更有用的是"少了第 51 行"——能据此定向重读那一页。"""
    r = check_sequence_continuity(_seq_items([i for i in range(1, 137) if i != 51]))
    assert r.verdict == "review" and r.missing == [51]


def test_widespread_gaps_block():
    r = check_sequence_continuity(
        _seq_items([i for i in range(1, 137) if i not in range(50, 70)]))
    assert r.verdict == "blocked" and len(r.missing) == 20


def test_no_seq_column_is_not_applicable_never_ok():
    """**「没有判据」不等于「没有问题」。** 四份实测文档一行序号都没有；
    若这里返回 ok，行数守恒就又变回同义反复了。"""
    r = check_sequence_continuity([{"seq": ""} for _ in range(136)])
    assert r.verdict == "not_applicable"
    assert r.verdict != "ok"
    assert "缺独立判据" in r.reason


def test_partial_coverage_refuses_to_extrapolate():
    """零星几个序号推不出整份的完整性——宁可说没有判据。"""
    items = _seq_items(range(1, 51)) + [{"seq": ""} for _ in range(86)]
    assert check_sequence_continuity(items).verdict == "not_applicable"


def test_does_not_assume_numbering_starts_at_one():
    """分部报价常按段重编号。只在**观测到的区间内**找缺口，不猜应有多少行。"""
    r = check_sequence_continuity(_seq_items(range(45, 137)))
    assert r.verdict == "ok" and r.observed_min == 45


def test_duplicate_seq_is_review_not_a_gap():
    """重复与缺口是两回事：缺口是丢行，重复可能是分部重编号（合法）。"""
    r = check_sequence_continuity(_seq_items([1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    assert r.verdict == "review" and r.duplicated == [2] and r.missing == []


def test_seq_text_variants_are_tolerated():
    r = check_sequence_continuity([{"seq": "1"}, {"seq": "2."}, {"seq": "No.3"},
                                   {"seq": "4"}, {"seq": "5"}])
    assert r.verdict == "ok"


def test_hierarchical_numbering_does_not_misjudge_chapter_gaps():
    """章节.序号形态（如 1.1..1.8, 2.1..2.8, 5.1..5.8）不能塌缩成 1/2/5 后
    误判"缺第 3/4 章"。这不是重复也不是丢行，是这套算法不支持的编号形态——
    如实说没有判据，不要输出一个自信但错误的 BLOCKED。

    实测：七份基准全是纯整数序号，没能测出这个盲区；按专业/分部分项组织的
    招标文件用这种编号并不罕见。
    """
    items = [{"seq": f"{ch}.{i}"} for ch in (1, 2, 5) for i in range(1, 9)]
    r = check_sequence_continuity(items)
    assert r.verdict == "not_applicable"
    assert r.missing == []


def test_single_hyphen_pair_below_threshold_still_gap_checked():
    """极少量的分段编号混在纯整数里不该触发降级——阈值是"大量"而不是"存在"。"""
    items = [{"seq": str(i)} for i in range(1, 30) if i != 15]
    items.append({"seq": "1-2"})  # 一条分段编号混入，占比远低于 30%
    r = check_sequence_continuity(items)
    assert r.verdict != "not_applicable", "个别分段编号不该让整份判定失效"
