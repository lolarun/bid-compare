"""Contract tests for EvaluationPolicyService (P1-1).

Locks the safety guarantees of get_evaluation_policy():
- Returns UNKNOWN when no confirmed policy exists (current behavior)
- UNKNOWN policy blocks auto-winner declaration
- UNKNOWN method/award_mode prevents downstream definitive conclusions
"""
from __future__ import annotations

from apps.api.services.evaluation_policy import (
    get_evaluation_policy,
    UNKNOWN_EVALUATION_POLICY,
    DEFAULT_EVALUATION_POLICY,
    EvaluationPolicy,
)


def test_get_policy_returns_unknown_for_any_project():
    """Before policy persistence is implemented, every project returns UNKNOWN."""
    for pid in (1, 42, 999, None):
        policy = get_evaluation_policy(pid)
        assert policy.method == "unknown"
        assert policy.award_mode == "unknown"


def test_unknown_policy_blocks_auto_winner():
    """can_auto_declare_winner must be False for UNKNOWN policy."""
    assert UNKNOWN_EVALUATION_POLICY.can_auto_declare_winner is False


def test_unknown_policy_no_factors():
    """UNKNOWN policy must carry no factors (empty tuple)."""
    assert UNKNOWN_EVALUATION_POLICY.factors == ()


def test_unknown_policy_no_weights():
    """UNKNOWN policy must not carry weights (prevents system from self-assigning)."""
    assert UNKNOWN_EVALUATION_POLICY.weights is None


def test_default_policy_requires_committee():
    """DEFAULT policy (project-66 confirmed): final decision requires committee."""
    assert DEFAULT_EVALUATION_POLICY.final_decision_requires_committee is True
    assert DEFAULT_EVALUATION_POLICY.can_auto_declare_winner is False


def test_policy_to_dict_includes_safety_flags():
    """to_dict() surface must include allows_split_award and can_auto_declare_winner."""
    d = UNKNOWN_EVALUATION_POLICY.to_dict()
    assert "allows_split_award" in d
    assert "can_auto_declare_winner" in d
    assert d["can_auto_declare_winner"] is False


def test_explicit_policy_can_declare_winner_only_when_all_gates_pass():
    """Synthetic: only lowest_price=True + weights set + no committee → can auto-declare."""
    policy = EvaluationPolicy(
        method="lowest_price",
        award_mode="single_supplier",
        lowest_price_wins=True,
        weights={"价格": 1.0},
        final_decision_requires_committee=False,
    )
    assert policy.can_auto_declare_winner is True
