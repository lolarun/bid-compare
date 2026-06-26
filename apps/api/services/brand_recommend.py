"""Brand recommendation service for 邀标建议.

Given a list of tender items, returns approved brands with historical
price statistics for each inferred category.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from apps.api.models import Material, Quote
from apps.api.models.brand_tier import BrandTier
from apps.api.services.quote_filters import valid_quote_filters
from apps.api.services.supplier_recommend import infer_categories


def recommend_brands(
    db: Session,
    tender_items: list[dict[str, Any]],
    top_n: int = 15,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return approved brand recommendations for the given tender items.

    Returns (recommendations, categories) where recommendations are sorted
    by sample_count desc (brands with more historical data ranked first).
    """
    categories = infer_categories(tender_items)
    if not categories:
        return [], []

    brand_records: list[BrandTier] = (
        db.query(BrandTier)
        .filter(
            BrandTier.is_approved == True,  # noqa: E712
            BrandTier.category.in_(categories),
        )
        .all()
    )
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
        })

    results.sort(key=lambda x: -x["sample_count"])
    return results[:top_n], categories


def _fetch_prices(db: Session, category: str, brand_aliases: list[str]) -> list[float]:
    from apps.api.models import Supplier
    rows = (
        db.query(Quote.unit_price)
        .join(Material, Material.id == Quote.material_id)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .filter(
            Material.category == category,
            Quote.brand.in_(brand_aliases),
            Quote.unit_price > 0,
            *valid_quote_filters(),
        )
        .all()
    )
    return sorted(r[0] for r in rows if r[0] and r[0] > 0)


def _percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = min(n - 1, int(n * p))
    return sorted_vals[idx]
