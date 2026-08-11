"""Regression tests for low-cost comparison policies."""

from apps.api.services.history.comparison import compare_price
from apps.api.services.history.comparison_profiles import get_comparison_profile


class _NoDatabaseAccess:
    def __getattr__(self, name):
        raise AssertionError(f"profile gate unexpectedly accessed db.{name}")


def test_panel_profile_uses_horizontal_comparison_only():
    profile = get_comparison_profile("配电箱")
    assert profile["key"] == "panel_horizontal"
    assert profile["history_baseline"] is False

    # The profile gate is evaluated before any historical DB query.
    result = compare_price(_NoDatabaseAccess(), category="配电箱", new_price=1234)
    assert result["comparison_profile"] == "panel_horizontal"
    assert result["reasonable_low"] is None
    assert result["deviation_pct"] is None
    assert "横向报价" in result["review_hint"]


def test_standard_profile_keeps_history_baseline_enabled():
    profile = get_comparison_profile("桥架")
    assert profile["key"] == "standard"
    assert profile["history_baseline"] is True
