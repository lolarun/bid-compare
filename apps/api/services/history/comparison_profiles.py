"""Small, versioned-in-code comparison policies for the pilot.

Profiles select evidence and review behavior; they do not replace the common
anchor/matrix engine and intentionally do not model a full BOM or ERP domain.
"""

from __future__ import annotations

from typing import TypedDict

from apps.api.core.config import COMPARISON_PROFILE_BY_CATEGORY


class ComparisonProfile(TypedDict):
    key: str
    history_baseline: bool
    review_hint: str


DEFAULT_PROFILE: ComparisonProfile = {
    "key": "standard",
    "history_baseline": True,
    "review_hint": "按同规格历史报价和本轮横向报价复核。",
}

def get_comparison_profile(category: str) -> ComparisonProfile:
    """Return the lowest-cost safe profile for a category."""
    return {**DEFAULT_PROFILE, **COMPARISON_PROFILE_BY_CATEGORY.get(category, {})}
