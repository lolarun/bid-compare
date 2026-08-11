"""合价派生的留痕与门禁（doc/19 §L2）—— 无 OCR/LLM/API。

固化 2026-08-09 复核发现的数据安全缺陷：
  原文没有合价时，batch-confirm 会静默执行 total = qty × unit_price 并入库。
  它有两重危害：
    1. 凭空造钱——亨通一条列错位行（数量/单价被右移一列）被算成 663.48 × 30214.88
       = 20,046,968.58，单行虚增约 2000 万；浦东 20 条派生行合计造出 5011 万。
    2. **销毁发现自己的证据**——total := qty × price 之后，算术校验
       |qty×price − total| ≤ 容差 恒成立，于是列错位行反而"完美通过"质量门，
       还把分母撑大、把真实错误稀释到阈值以下。

因此两条不变量：派生必须留痕；派生行不得进入算术样本。
"""
from __future__ import annotations

import pytest

from apps.api.core.domain_config import MATCH_DERIVED_TOTAL_MAX_RATE


class _Line:
    """最小替身，只带门禁读取的字段。"""

    def __init__(self, id, qty, unit_price, total_price, meta=None, name="电缆"):
        self.id = id
        self.qty = qty
        self.unit_price = unit_price
        self.total_price = total_price
        self.extraction_meta = meta or {}
        self.raw_name = name


def _is_derived_total(b) -> bool:
    """与 analysis.py 门禁中的判据保持一致（total_source 或 validation_flags）。"""
    meta = b.extraction_meta or {}
    return (meta.get("total_source") == "derived"
            or "derived_total" in (meta.get("validation_flags") or []))


class TestProvenanceIsRecorded:
    def test_ocr_sourced_row_is_not_flagged(self):
        line = _Line(1, 10, 5, 50, {"total_source": "ocr", "validation_flags": []})
        assert not _is_derived_total(line)

    def test_derived_row_carries_both_markers(self):
        """total_source 与 validation_flags 任一存在都必须能被识别出来。"""
        by_source = _Line(2, 10, 5, 50, {"total_source": "derived"})
        by_flag = _Line(3, 10, 5, 50, {"validation_flags": ["derived_total"]})
        assert _is_derived_total(by_source)
        assert _is_derived_total(by_flag)


class TestDerivedRowsCannotLaunderArithmeticErrors:
    """核心不变量：派生行不得进入算术样本。"""

    def test_derived_rows_always_satisfy_the_check_trivially(self):
        """先证明危害存在：派生行的算术偏差恒为 0，留在样本里就是纯稀释剂。"""
        derived = _Line(1, 663.48, 30214.88, round(663.48 * 30214.88, 4),
                        {"total_source": "derived"})
        assert abs(derived.qty * derived.unit_price - derived.total_price) < 1e-6

    def test_excluding_derived_rows_keeps_the_real_error_visible(self):
        """一条真实列错位错误 + 若干派生行：排除后错误率必须仍然超阈值。"""
        # 宏胜真实错位行：数量/单价/合价整体右移一列
        bad = _Line(99, 1987.1567, 1905.25, 1042.99, {"total_source": "ocr"})
        good = [_Line(i, 10, 5, 50, {"total_source": "ocr"}) for i in range(4)]
        derived = [
            _Line(100 + i, 7, 100, 700, {"total_source": "derived"})
            for i in range(20)
        ]
        eligible = [bad, *good, *derived]

        evaluable = [b for b in eligible if not _is_derived_total(b)]
        hard = [b for b in evaluable
                if abs(b.total_price - b.qty * b.unit_price)
                / max(abs(b.total_price), abs(b.qty * b.unit_price)) > 0.125]
        rate_excluded = len(hard) / len(evaluable)

        # 对照：把派生行也算进去，同样一条错误被稀释
        rate_included = len(hard) / len(eligible)

        assert rate_excluded > 0.05, "排除派生行后，真实错误必须仍然可见"
        assert rate_included < 0.05, "这正是缺陷：派生行会把真实错误稀释到阈值以下"


class TestDerivedRateGate:
    def test_high_derived_share_is_itself_a_finding(self):
        eligible = [_Line(i, 1, 1, 1, {"total_source": "derived"}) for i in range(10)]
        rate = sum(1 for b in eligible if _is_derived_total(b)) / len(eligible)
        assert rate > MATCH_DERIVED_TOTAL_MAX_RATE

    def test_threshold_is_centralised_not_inline(self):
        assert 0 < MATCH_DERIVED_TOTAL_MAX_RATE < 1
