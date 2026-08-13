"""声明总价闭环门：提交前阻断，幂等路径不得绕过。

背景（2026-08-09 复核发现）：这道校验原本在 `db.commit()` **之后**执行、阈值 5%、
只写 `job.result` 不阻断。实测方向判错一页造成 0.63%（129,532 元）的偏差会被判 pass
并正常入库——等于没有门。
"""
from __future__ import annotations

import pytest

from apps.api.core.domain_config import CHECKSUM_BLOCK_DELTA_RATIO
from apps.api.services.submission.quote_confirmation_service import _build_checksum


class _Job:
    def __init__(self, declared):
        self.result = {"_doc_meta": {"bid_total": declared}} if declared is not None else {}


def test_exact_match_passes():
    cs = _build_checksum(_Job(20_597_048.33), 20_597_048.33, 136)
    assert cs["status"] == "pass" and cs["delta_pct"] == 0.0


def test_rounding_level_difference_passes():
    """136 行两位小数的累积舍入在 2000 万上不到百万分之一，必须放行。"""
    cs = _build_checksum(_Job(20_597_048.33), 20_597_048.37, 136)
    assert cs["status"] == "pass"


def test_orientation_grade_error_is_blocked():
    """实测方向判错一页 = 129,532 元 = 0.63%。旧的 5% 阈值会放行，必须拦住。"""
    declared = 20_597_048.33
    cs = _build_checksum(_Job(declared), declared - 129_532.01, 136)
    assert cs["status"] == "fail"
    assert cs["delta_pct"] == pytest.approx(0.629, abs=0.01)


def test_threshold_boundary():
    declared = 1_000_000.0
    just_inside = declared * (1 - CHECKSUM_BLOCK_DELTA_RATIO)
    just_outside = declared * (1 - CHECKSUM_BLOCK_DELTA_RATIO * 1.01)
    assert _build_checksum(_Job(declared), just_inside, 10)["status"] == "pass"
    assert _build_checksum(_Job(declared), just_outside, 10)["status"] == "fail"


def test_missing_declared_total_is_unknown_not_pass():
    """文件没给声明总价 = 我们没有这个证据，**不等于校验通过**。"""
    cs = _build_checksum(_Job(None), 12345.0, 10)
    assert cs["status"] == "unknown"
    assert cs["status"] != "pass"
    assert cs["reason"]


def test_zero_or_garbage_declared_total_is_unknown():
    for bad in (0, -1, "", "n/a"):
        assert _build_checksum(_Job(bad), 100.0, 10)["status"] == "unknown"


def test_no_lines_is_unknown():
    assert _build_checksum(_Job(1000.0), 0.0, 0)["status"] == "unknown"


def test_checksum_reports_threshold_for_audit():
    """响应里要带上判据本身，否则事后无法解释为什么拦/不拦。"""
    cs = _build_checksum(_Job(1000.0), 900.0, 10)
    assert cs["threshold_pct"] == pytest.approx(CHECKSUM_BLOCK_DELTA_RATIO * 100)
    assert cs["declared"] == 1000.0 and cs["line_sum"] == 900.0
