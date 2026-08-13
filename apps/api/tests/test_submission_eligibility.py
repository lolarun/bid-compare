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

    def __init__(self, unit_price=100.0, total_source="ocr", flags=None):
        self.id = _Line._next
        _Line._next += 1
        self.unit_price = unit_price
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


def test_column_shift_one_row_blocks():
    """列错位没有合法形态，一行即整份不可用。"""
    v = _verdict([_Line(), _Line(flags=["column_shift"])])
    assert v.verdict == BLOCKED
    assert any(r.code == "column_shift" for r in v.reasons)


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
