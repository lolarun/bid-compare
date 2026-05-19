"""SupplierRecommendService — recommend invitees for a tender.

Algorithm (4-dim weighted score per plan §6):
- history_count (30%): how many times this supplier has been quoted in the
  categories represented in the tender items
- price_advantage (25%): average historical deviation (negative is better)
- overall_score (25%): the existing 5-dim score_supplier() result
- brand_score (20%): brand-tier hit rate from score_supplier()

Cold-start fallback: if candidates < top_n, append top non-candidate
suppliers by total_score so the user gets at least top_n recommendations.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, PROFESSION_MAP
from apps.api.services import scoring


ALL_CATEGORIES = set(PROFESSION_MAP.keys())


def infer_categories(tender_items: list[dict[str, Any]]) -> list[str]:
    """Best-effort inference of categories present in the tender.

    Rules:
    - Use `category` field if provided AND it matches a known category
    - Else scan the `name` for a known category keyword
    - Deduplicate; preserve insertion order
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
            if cat in name and cat not in seen:
                out.append(cat)
                seen.add(cat)
                break
    return out


def recommend_suppliers(
    db: Session,
    tender_items: list[dict[str, Any]],
    top_n: int = 5,
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    """Recommend up to `top_n` suppliers for the given tender items.

    Returns: list of dicts with supplier_id, supplier_name, score (0-100),
    rank, and a reason dict explaining the score.
    """
    if top_n <= 0:
        return []
    categories = infer_categories(tender_items)

    # ── 1. Recall candidates: suppliers with quote history in target categories ─
    candidates: list[Supplier] = []
    if categories:
        candidates = (
            db.query(Supplier)
            .join(Quote, Supplier.id == Quote.supplier_id)
            .join(Material, Material.id == Quote.material_id)
            .filter(
                Material.category.in_(categories),
                Quote.unit_price > 0,
            )
            .distinct()
            .all()
        )

    # ── 2. Score each candidate ───────────────────────────────────────────────
    scored: list[dict[str, Any]] = []
    for sup in candidates:
        scored.append(_score_one(db, sup, categories))

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    primary = scored[:top_n]

    # ── 3. Cold-start fallback: if too few candidates, extend with global pool ─
    if len(primary) < top_n:
        primary_ids = {s["supplier_id"] for s in primary}
        all_others = (
            db.query(Supplier)
            .filter(~Supplier.id.in_(primary_ids))
            .all()
            if primary_ids
            else db.query(Supplier).all()
        )
        # Score the global pool without category restriction
        extras = [_score_one(db, sup, []) for sup in all_others]
        extras.sort(key=lambda x: x["score"], reverse=True)
        primary.extend(extras[: top_n - len(primary)])

    # ── 4. Assign ranks ───────────────────────────────────────────────────────
    for i, entry in enumerate(primary):
        entry["rank"] = i + 1
    return primary


def _score_one(
    db: Session,
    sup: Supplier,
    categories: list[str],
) -> dict[str, Any]:
    """Compute 4-dim weighted score + reason payload for a single supplier."""
    # ── History count in target categories ──
    history_q = db.query(func.count(Quote.id)).filter(
        Quote.supplier_id == sup.id, Quote.unit_price > 0
    )
    if categories:
        history_q = history_q.join(Material).filter(Material.category.in_(categories))
    history_count = int(history_q.scalar() or 0)
    history_score = min(100.0, history_count * 2.0)  # 50 quotes → 100

    # ── Price advantage: average deviation_pct (negative = better) ──
    dev_q = db.query(func.avg(Quote.deviation_pct)).filter(
        Quote.supplier_id == sup.id,
        Quote.deviation_pct.isnot(None),
    )
    if categories:
        dev_q = dev_q.join(Material).filter(Material.category.in_(categories))
    avg_dev = dev_q.scalar()
    avg_dev_f = float(avg_dev) if avg_dev is not None else None
    # Map deviation to 0-100: dev = -10% → 100, 0% → 50, +20% → 0
    if avg_dev_f is None:
        price_score = 50.0  # no signal → neutral
    else:
        # Clamp [-0.10, 0.20] then linear-map to [100, 0]
        x = max(-0.10, min(0.20, avg_dev_f))
        price_score = (0.20 - x) / 0.30 * 100.0

    # ── Overall 5-dim score (reuses scoring.score_supplier) ──
    try:
        # Pass a representative category if multiple — first one — else None
        cat_for_overall = categories[0] if categories else None
        overall_obj = scoring.score_supplier(db, sup.id, cat_for_overall)
        overall_score = overall_obj["total_score"]
        brand_score = overall_obj["brand_score"]
    except Exception:
        overall_score = 50.0
        brand_score = 70.0

    total = (
        0.30 * history_score
        + 0.25 * price_score
        + 0.25 * overall_score
        + 0.20 * brand_score
    )

    summary_parts: list[str] = []
    if categories:
        summary_parts.append(
            f"在 {len(categories)} 个品类成交 {history_count} 次"
        )
    else:
        summary_parts.append(f"历史成交 {history_count} 次")
    if avg_dev_f is not None:
        sign = "+" if avg_dev_f >= 0 else ""
        summary_parts.append(f"平均偏差 {sign}{avg_dev_f:.1%}")
    summary_parts.append(f"综合评分 {overall_score:.0f}")

    return {
        "supplier_id": sup.id,
        "supplier_name": sup.name,
        "score": round(total, 1),
        "rank": 0,  # filled by caller
        "reason": {
            "history_count": history_count,
            "history_score": round(history_score, 1),
            "avg_deviation_pct": round(avg_dev_f, 4) if avg_dev_f is not None else None,
            "price_score": round(price_score, 1),
            "overall_score": round(overall_score, 1),
            "brand_score": round(brand_score, 1),
            "summary": " · ".join(summary_parts),
        },
    }
