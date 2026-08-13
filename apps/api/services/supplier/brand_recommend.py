"""Brand recommendation service for 邀标建议.

Given a list of tender items, returns approved brands with historical
price statistics for each inferred category.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

import math

from apps.api.models import Material, Quote
from apps.api.models.brand_tier import BrandTier
from apps.api.services.history.quote_filters import valid_quote_filters
from apps.api.services.supplier.supplier_recommend import infer_categories

# Scoring constants — see docs/design/15-invite-brand-recommendation.md §2
W_TIER = 0.30        # joint-venture soft preference
W_DATA = 0.70        # data-confidence majority weight
MAX_SAMPLES = 50     # normalisation ceiling for data_factor


def _score(tier: str, sample_count: int) -> float:
    tier_factor = 1.0 if tier == "合资" else 0.0
    data_factor = math.log(sample_count + 1) / math.log(MAX_SAMPLES + 1)
    data_factor = min(data_factor, 1.0)
    return W_TIER * tier_factor + W_DATA * data_factor


def recommend_brands(
    db: Session,
    tender_items: list[dict[str, Any]],
    top_n: int = 15,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return approved brand recommendations for the given tender items.

    Ranked by composite score: 0.30 × tier_factor + 0.70 × log-normalised sample_count.
    See docs/design/15-invite-brand-recommendation.md §2.
    """
    categories = infer_categories(tender_items)
    if not categories:
        return [], []

    brand_records: list[BrandTier] = db.scalars(
        select(BrandTier).where(
            BrandTier.is_approved == True,  # noqa: E712
            BrandTier.category.in_(categories),
        )
    ).all()
    if not brand_records:
        return [], categories

    # Group by (canonical_name, category) to aggregate aliases
    canon_rep: dict[tuple[str, str], BrandTier] = {}
    alias_map: dict[tuple[str, str], list[str]] = defaultdict(list)
    for bt in brand_records:
        canon = bt.canonical_name or bt.brand_name
        key = (canon, bt.category)
        if key not in canon_rep:
            canon_rep[key] = bt
        alias_map[key].append(bt.brand_name)

    results: list[dict[str, Any]] = []
    for (canon, cat), bt in canon_rep.items():
        aliases = alias_map[(canon, cat)]
        price_vals = _fetch_prices(db, cat, aliases)
        n = len(price_vals)

        median = _percentile(price_vals, 0.50) if n else None
        p10    = _percentile(price_vals, 0.10) if n else None
        p90    = _percentile(price_vals, 0.90) if n else None

        tags: list[str] = []
        tags.append("合资品牌" if bt.tier == "合资" else "国产品牌")
        if n >= 20:
            tags.append("数据充足")
        elif n >= 5:
            tags.append("有参考价格")

        results.append({
            "brand_name":   canon,
            "tier":         bt.tier,
            "category":     cat,
            "sample_count": n,
            "price_median": round(median, 2) if median is not None else None,
            "price_p10":    round(p10, 2)    if p10    is not None else None,
            "price_p90":    round(p90, 2)    if p90    is not None else None,
            "tags":         tags,
            "_score":       _score(bt.tier, n),
        })

    results.sort(key=lambda x: -x["_score"])
    for r in results:
        del r["_score"]
    return results[:top_n], categories


def _fetch_prices(db: Session, category: str, brand_aliases: list[str]) -> list[float]:
    from apps.api.models import Supplier
    rows = db.scalars(
        select(Quote.unit_price)
        .join(Material, Material.id == Quote.material_id)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .where(
            Material.category == category,
            Quote.brand.in_(brand_aliases),
            Quote.unit_price > 0,
            *valid_quote_filters(),
        )
    ).all()
    return sorted(price for price in rows if price and price > 0)


def _percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = min(n - 1, int(n * p))
    return sorted_vals[idx]
