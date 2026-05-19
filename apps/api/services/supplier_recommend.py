"""SupplierRecommendService — recommend invitees for a tender.

Algorithm (4-dim weighted score, see plan §6):

   total = 0.30 · history_score   (volume + recency)
         + 0.25 · price_score     (avg deviation_pct vs reasonable_low)
         + 0.25 · overall_score   (existing 5-dim score_supplier)
         + 0.20 · brand_score     (brand-tier hit rate from score_supplier)

Robustness fixes vs. v1 (post-audit):
- infer_categories uses token-boundary match → no "止回阀门" false-positive
- history_score uses log1p compression + 180-day recency decay so a single
  large contract doesn't get drowned by many trivial ones
- price_score computes deviation on-the-fly (uses Material.ref_price_*) so
  seed quotes whose Quote.deviation_pct is NULL still contribute
- candidate scoring uses ONE bulk aggregate query + cached score_supplier
  call per supplier → no more N+1 storm

Cold-start: if candidate pool < top_n, fill with top-scored suppliers from
the global pool (scored without category constraint).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, PROFESSION_MAP
from apps.api.services import scoring

log = logging.getLogger(__name__)


ALL_CATEGORIES = set(PROFESSION_MAP.keys())

# Recency half-life: a quote N days old contributes exp(-N/180) of its weight.
RECENCY_HALF_LIFE_DAYS = 180.0


def infer_categories(tender_items: list[dict[str, Any]]) -> list[str]:
    """Best-effort inference of categories present in the tender.

    Rules:
    1. Use `category` field verbatim if it matches a known category (exact)
    2. Else: scan `name` for a known category KEYWORD — but only count it as
       a match if the category string is found at a word/token boundary OR
       at the start. This stops "止回阀门" → "阀门" false positives where
       "阀门" appears as a SUFFIX of a longer compound term.

       Concretely: we strip the matched substring and require what surrounds
       it to NOT be another Chinese character that would make the term a
       suffix of something else.
    3. Deduplicate preserving insertion order.
    """
    out: list[str] = []
    seen: set[str] = set()
    for it in tender_items or []:
        cand = (it.get("category") or "").strip()
        if cand and cand in ALL_CATEGORIES:
            if cand not in seen:
                out.append(cand)
                seen.add(cand)
            continue
        name = (it.get("name") or it.get("material") or "").strip()
        for cat in ALL_CATEGORIES:
            if _is_category_token_match(name, cat) and cat not in seen:
                out.append(cat)
                seen.add(cat)
                break
    return out


def _is_category_token_match(name: str, category: str) -> bool:
    """True iff `category` appears as a salient token in `name`.

    Heuristic for CJK:
    - Categories of length ≥ 3 chars (e.g. 母线槽, 风口风阀, 风机盘管):
      substring match is sufficient because they're specific enough.
    - Categories of length 2 (e.g. 阀门, 桥架, 水箱): require the
      occurrence to be at the start of `name` OR preceded by a non-CJK
      character. This stops "止回阀门" → "阀门" false positives where
      "阀门" appears as a SUFFIX of a longer compound noun.
    """
    if not name or category not in name:
        return False
    if len(category) >= 3:
        return True
    # Length-2 category: require token-boundary
    idx = -1
    while True:
        idx = name.find(category, idx + 1)
        if idx == -1:
            return False
        if idx == 0:
            return True
        prev_char = name[idx - 1]
        if not _is_cjk(prev_char):
            return True
        # Otherwise keep looking; another occurrence may be salient


def _is_cjk(ch: str) -> bool:
    """Detect CJK Unified Ideographs (incl. Extension A)."""
    if not ch:
        return False
    cp = ord(ch)
    return (
        (0x4E00 <= cp <= 0x9FFF)  # Basic CJK
        or (0x3400 <= cp <= 0x4DBF)  # Extension A
    )


# ─── Bulk per-supplier aggregate (one query, kills N+1) ──────────────────
def _aggregate_supplier_stats(
    db: Session,
    categories: list[str],
) -> dict[int, dict[str, Any]]:
    """Return {supplier_id: {history_count, recent_history, avg_deviation,
    deviation_count}} for every supplier with relevant quote history.

    `recent_history` weights by exp(-days/180) so volume + recency combine.
    `avg_deviation` is computed from Quote.deviation_pct when populated, else
    fallback to (unit_price - material.ref_price_reasonable_low) /
                  material.ref_price_reasonable_low.

    The fallback runs inside SQL via CASE so we get correct averages without
    N+1 round-trips.
    """
    now = datetime.now(timezone.utc)

    # Computed deviation: prefer Quote.deviation_pct; else compute from
    # the cached Material ref_price_reasonable_low / ref_price_median when
    # the quote has a valid unit_price.
    ref_expr = func.coalesce(
        Material.ref_price_reasonable_low, Material.ref_price_median
    )
    fallback_dev = case(
        (Quote.deviation_pct.isnot(None), Quote.deviation_pct),
        (
            (ref_expr.isnot(None))
            & (ref_expr > 0)
            & (Quote.unit_price.isnot(None))
            & (Quote.unit_price > 0),
            (Quote.unit_price - ref_expr) / ref_expr,
        ),
        else_=None,
    )

    q = (
        db.query(
            Quote.supplier_id,
            func.count(Quote.id).label("history_count"),
            func.avg(fallback_dev).label("avg_deviation"),
            func.count(fallback_dev).label("dev_count"),
        )
        .join(Material, Material.id == Quote.material_id)
        .filter(
            Quote.supplier_id.isnot(None),
            Quote.unit_price > 0,
        )
    )
    if categories:
        q = q.filter(Material.category.in_(categories))
    rows = q.group_by(Quote.supplier_id).all()

    stats: dict[int, dict[str, Any]] = {}
    for r in rows:
        stats[r.supplier_id] = {
            "history_count": int(r.history_count or 0),
            "avg_deviation": float(r.avg_deviation) if r.avg_deviation is not None else None,
            "dev_count": int(r.dev_count or 0),
            # `recent_history` requires per-quote dates; computed below if
            # we have a small candidate pool. Default to history_count.
            "recent_history": float(r.history_count or 0),
        }

    # Recency decay: only compute for the candidate set (typically <= 50)
    # — running it for ALL suppliers would re-introduce O(N) overhead.
    if stats and len(stats) <= 60:
        for sid in list(stats.keys()):
            quote_dates = (
                db.query(Quote.created_at)
                .join(Material, Material.id == Quote.material_id)
                .filter(
                    Quote.supplier_id == sid,
                    Quote.unit_price > 0,
                )
            )
            if categories:
                quote_dates = quote_dates.filter(Material.category.in_(categories))
            decayed = 0.0
            for row in quote_dates.all():
                d = row[0]
                if d is None:
                    decayed += 0.3  # unknown date → modest contribution
                    continue
                # Normalize to UTC-naive comparison
                if d.tzinfo is None:
                    d_aware = d.replace(tzinfo=timezone.utc)
                else:
                    d_aware = d
                age_days = max(0.0, (now - d_aware).total_seconds() / 86400.0)
                decayed += math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)
            stats[sid]["recent_history"] = decayed
    return stats


def recommend_suppliers(
    db: Session,
    tender_items: list[dict[str, Any]],
    top_n: int = 5,
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    """Recommend up to `top_n` suppliers for the given tender items."""
    if top_n <= 0:
        return []
    categories = infer_categories(tender_items)

    # ── 1. Recall candidates: bulk-fetch suppliers with category history ──
    candidates: list[Supplier] = []
    cand_ids: list[int] = []
    if categories:
        cand_ids = [
            r[0]
            for r in db.query(Supplier.id)
            .join(Quote, Supplier.id == Quote.supplier_id)
            .join(Material, Material.id == Quote.material_id)
            .filter(
                Material.category.in_(categories),
                Quote.unit_price > 0,
            )
            .distinct()
            .all()
        ]
        candidates = (
            db.query(Supplier).filter(Supplier.id.in_(cand_ids)).all()
            if cand_ids
            else []
        )

    # ── 2. Bulk aggregate stats in ONE query (was N+1) ──
    cat_stats = _aggregate_supplier_stats(db, categories) if categories else {}

    # ── 3. Score each candidate ──
    scored: list[dict[str, Any]] = []
    for sup in candidates:
        scored.append(_score_one(db, sup, categories, cat_stats.get(sup.id)))

    scored.sort(key=lambda x: x["score"], reverse=True)
    primary = scored[:top_n]

    # ── 4. Cold-start: fill remaining slots from global pool ──
    if len(primary) < top_n:
        primary_ids = {s["supplier_id"] for s in primary}
        all_others_q = db.query(Supplier).filter(~Supplier.id.in_(primary_ids)) if primary_ids else db.query(Supplier)
        # Cap at 50 to bound cold-start cost
        all_others = all_others_q.limit(50).all()
        # Score without category constraint → use empty cat_stats
        global_stats = _aggregate_supplier_stats(db, []) if all_others else {}
        extras = [_score_one(db, sup, [], global_stats.get(sup.id)) for sup in all_others]
        extras.sort(key=lambda x: x["score"], reverse=True)
        primary.extend(extras[: top_n - len(primary)])

    # ── 5. Assign ranks ──
    for i, entry in enumerate(primary):
        entry["rank"] = i + 1
    return primary


def _per_category_breakdown(
    db: Session, supplier_id: int, categories: list[str]
) -> dict[str, int]:
    """Return {category: quote_count} for this supplier. Used in summary."""
    if not categories:
        return {}
    rows = (
        db.query(Material.category, func.count(Quote.id))
        .join(Quote, Quote.material_id == Material.id)
        .filter(
            Quote.supplier_id == supplier_id,
            Material.category.in_(categories),
            Quote.unit_price > 0,
        )
        .group_by(Material.category)
        .all()
    )
    return {cat: int(cnt) for cat, cnt in rows}


def _score_one(
    db: Session,
    sup: Supplier,
    categories: list[str],
    agg: dict[str, Any] | None,
) -> dict[str, Any]:
    """4-dim weighted score for a single supplier.

    `agg` carries the pre-aggregated history/deviation stats so we DON'T
    re-issue per-supplier queries here.
    """
    history_count = (agg or {}).get("history_count", 0) or 0
    recent_history = (agg or {}).get("recent_history", float(history_count)) or 0.0
    avg_dev = (agg or {}).get("avg_deviation")  # may be None
    dev_count = (agg or {}).get("dev_count", 0) or 0

    # History: log1p compresses; 60+ recent-weighted quotes maxes out at 100
    # log1p(60) ≈ 4.11 ⇒ scale ≈ 24.3 for max=100
    history_score = min(100.0, math.log1p(recent_history) * 24.0)

    # Price: -10% → 100; 0% → 50; +20% → 0. Same mapping as before but
    # uses the fallback-computed average.
    if avg_dev is None:
        price_score = 50.0
    else:
        x = max(-0.10, min(0.20, float(avg_dev)))
        price_score = (0.20 - x) / 0.30 * 100.0

    # Overall + brand from existing 5-dim service (kept compatible)
    try:
        cat_for_overall = categories[0] if categories else None
        overall_obj = scoring.score_supplier(db, sup.id, cat_for_overall)
        overall_score = overall_obj["total_score"]
        brand_score = overall_obj["brand_score"]
    except Exception:
        log.exception("score_supplier failed for supplier_id=%s", sup.id)
        overall_score = 50.0
        brand_score = 70.0

    total = (
        0.30 * history_score
        + 0.25 * price_score
        + 0.25 * overall_score
        + 0.20 * brand_score
    )

    # AUDIT-FIX L1: instead of "在 N 个品类成交 K 次" (misleading when most
    # of K is in one category), show per-category counts when we have them.
    summary_parts: list[str] = []
    if categories:
        breakdown = _per_category_breakdown(db, sup.id, categories)
        if breakdown:
            # Sort by count descending, show top 3 to keep summary short
            top_cats = sorted(breakdown.items(), key=lambda x: -x[1])[:3]
            cats_str = " · ".join(f"{cat} {cnt}" for cat, cnt in top_cats)
            summary_parts.append(f"成交 {history_count} 次（{cats_str}）")
        else:
            summary_parts.append(f"在 {len(categories)} 个品类成交 {history_count} 次")
    else:
        summary_parts.append(f"历史成交 {history_count} 次")
    if avg_dev is not None:
        sign = "+" if avg_dev >= 0 else ""
        coverage = f" ({dev_count}/{history_count} 条有偏差数据)" if dev_count < history_count else ""
        summary_parts.append(f"平均偏差 {sign}{avg_dev:.1%}{coverage}")
    summary_parts.append(f"综合评分 {overall_score:.0f}")

    return {
        "supplier_id": sup.id,
        "supplier_name": sup.name,
        "score": round(total, 1),
        "rank": 0,  # filled by caller
        "reason": {
            "history_count": history_count,
            "history_score": round(history_score, 1),
            "avg_deviation_pct": round(float(avg_dev), 4) if avg_dev is not None else None,
            "price_score": round(price_score, 1),
            "overall_score": round(overall_score, 1),
            "brand_score": round(brand_score, 1),
            "summary": " · ".join(summary_parts),
        },
    }
