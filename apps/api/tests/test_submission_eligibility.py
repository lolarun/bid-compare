"""统一资格判据：这份报价能不能进正式比价。

背景：此前三处各自判断——resolve 只看 status；bid_matrix 读 checksum 但 unknown 当通过；
match 门有自己的 6 项检查却完全不看 checksum。**同一个问题在不同入口得到不同答案，
就等于没有判据。**
"""
from __future__ import annotations

import pytest

from apps.api.services.ingestion.draft_integrity import BLOCKED, OK, REVIEW
from apps.api.services.submission.submission_eligibility import (
    blocking_summary,
    evaluate_submission,
)


class _Line:
    _next = 1

    def __init__(self, unit_price=100.0, total_source="ocr", flags=None,
                 total_price=100.0):
        self.id = _Line._next
        _Line._next += 1
        self.unit_price = unit_price
        # 列错位判据数的是"因错位丢了合价"的行，所以这个字段必须能单独设（design/34）
        self.total_price = total_price
        self.extraction_meta = {"total_source": total_source,
                                "validation_flags": list(flags or [])}


class _Sub:
    def __init__(self, status="pending", job_id="J1"):
        self.id = 7
        self.status = status
        self.job_id = job_id


class _Job:
    def __init__(self, checksum):
        self.result = {"_checksum": checksum} if checksum is not None else {}


class _DB:
    """最小替身：evaluate_submission 只需要 get(ExtractionJob) 和 scalars(select(lines))。"""

    def __init__(self, lines, job):
        self._lines, self._job = lines, job

    def get(self, _model, _pk):
        return self._job

    def scalars(self, _stmt):
        class _R:
            def __init__(self, items):
                self._items = items

            def all(self):
                return self._items
        return _R(self._lines)


def _verdict(lines, checksum={"status": "pass"}, status="pending"):
    return evaluate_submission(_DB(lines, _Job(checksum)), _Sub(status=status))


def test_clean_submission_is_eligible():
    v = _verdict([_Line(), _Line()])
    assert v.verdict == OK and v.eligible and v.clean
    assert v.stats["line_count"] == 2


def test_superseded_is_blocked():
    v = _verdict([_Line()], status="superseded")
    assert v.verdict == BLOCKED and not v.eligible
    assert any(r.code == "submission_status" for r in v.reasons)


def test_checksum_fail_blocks():
    """此前 match 门完全不看 checksum，一份闭环失败的报价能直接进矩阵。"""
    v = _verdict([_Line()], checksum={"status": "fail", "delta_pct": 0.63})
    assert v.verdict == BLOCKED
    assert any(r.code == "checksum_failed" for r in v.reasons)


def test_checksum_unknown_is_review_visible_but_not_blocking():
    """没有声明总价 = 缺证据，**不是有缺陷**。

    很多报价单本来就不写总计。当不可用会拦掉绝大多数正常文档；当通过又会让缺口
    隐形。正确处置是放行 + 强制可见（reasons 里必须有它）。
    """
    v = _verdict([_Line()], checksum={"status": "unknown"})
    assert v.verdict == REVIEW
    assert v.eligible, "缺证据不等于有缺陷，不应阻断"
    assert not v.clean, "但必须留下疑点，不能当成干净"
    assert any(r.code == "checksum_unknown" for r in v.reasons)


def test_missing_checksum_key_is_also_review():
    v = _verdict([_Line()], checksum=None)
    assert v.verdict == REVIEW and v.eligible and not v.clean


def test_column_shift_few_rows_is_review_not_blocked():
    """少量列错位只让那些行进复核，不牵连整份（docs/design/34，2026-08-22 改）。

    原判据是"一行即整份不可用"。识别侧补上位移检测后它真的会触发了——七份语料四份
    中招，其中两份只因为 1 行；一行毙掉整份，用户连另外一百多行都看不到。改用
    `domain_config` 里本来就写着这个意思的比例/绝对数双闸门。
    """
    v = _verdict([_Line(), _Line(flags=["column_shift"], total_price=None)])
    assert v.verdict == REVIEW
    assert v.eligible and not v.clean          # 可用，但必须留下疑点
    assert any(r.code == "column_shift" for r in v.reasons)


def test_column_shift_above_ratio_blocks_whole_submission():
    """错位行占比过高 = 整份结构不可靠，这时才 BLOCKED。"""
    lines = ([_Line() for _ in range(6)]
             + [_Line(flags=["column_shift"], total_price=None) for _ in range(4)])
    v = _verdict(lines)                        # 4/10 = 40%，且 >= 绝对数下限
    assert v.verdict == BLOCKED
    assert any(r.code == "column_shift" for r in v.reasons)


def test_column_shift_below_absolute_count_stays_review():
    """比例超标但绝对行数太少（小表被比例稀释的反面）——不牵连整份。"""
    lines = [_Line(), _Line(), _Line(flags=["column_shift"], total_price=None)]  # 1/3 超比例，但只有 1 行
    v = _verdict(lines)
    assert v.verdict == REVIEW and v.eligible


def test_missing_total_blocks():
    v = _verdict([_Line(), _Line(total_source="missing")])
    assert v.verdict == BLOCKED
    assert any(r.code == "missing_total" for r in v.reasons)


def test_not_quoted_rows_do_not_block():
    """明确不报价的行是合法事实，不阻断，但要计数报出来。"""
    lines = [_Line() for _ in range(8)] + [
        _Line(unit_price=None, total_source="not_quoted") for _ in range(2)]
    v = _verdict(lines)
    assert v.verdict == OK and v.eligible and v.clean
    assert v.stats["not_quoted_rows"] == 2


def test_coverage_is_left_to_the_match_gate():
    """覆盖率**有意不在这里**：match 门已有且正确处理了合计行排除。
    在这里再写一遍会得到两套语义略有差异的实现，正是本模块要消除的问题。"""
    lines = [_Line() for _ in range(5)] + [_Line(unit_price=None) for _ in range(5)]
    v = _verdict(lines)
    assert v.eligible, "低覆盖率不由本模块阻断"
    assert not any(r.code == "price_coverage" for r in v.reasons)


def test_no_lines_blocks():
    v = _verdict([])
    assert v.verdict == BLOCKED
    assert any(r.code == "no_lines" for r in v.reasons)


def test_multiple_problems_all_reported():
    """每份报价为什么不能用必须逐条可见——合并成一句话就没法处理了。"""
    v = _verdict([_Line(flags=["column_shift"]), _Line(total_source="missing")],
                 checksum={"status": "fail", "delta_pct": 3.0})
    codes = {r.code for r in v.reasons}
    assert {"checksum_failed", "column_shift", "missing_total"} <= codes


def test_blocking_summary_only_lists_ineligible():
    good = _verdict([_Line()])
    bad = _verdict([_Line()], status="rejected")
    out = blocking_summary([good, bad])
    assert len(out) == 1 and out[0]["verdict"] == BLOCKED


def test_column_shift_that_kept_its_total_is_not_structural():
    """错位但金额保住了 → 不计入"结构不可靠"的分子（docs/design/34）。

    实测来源：一份 89 行报价有 6 行错位（单位掉进数量槽），但那 6 行的金额通过算术
    自洽校验、对标准答案 6/6 全对。按"触发检测器就算"会让它 6.7% 超阈值、整份被拦，
    连预览都进不去；按"丢了合价才算"则占比 0，正常放行。
    """
    lines = [_Line() for _ in range(4)] + [_Line(flags=["column_shift"]) for _ in range(4)]
    v = _verdict(lines)                      # 4/8 触发，但一行都没丢合价
    assert v.verdict == REVIEW and v.eligible
