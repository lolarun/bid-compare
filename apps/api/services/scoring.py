"""Supplier scoring and multi-supplier comparison service — v2."""

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, BrandTier, AnalysisConfig, DEFAULT_SCORING_WEIGHTS


def get_scoring_weights(db: Session) -> dict:
    cfg = db.query(AnalysisConfig).filter(AnalysisConfig.key == "scoring_weights").first()
    return cfg.value if cfg else DEFAULT_SCORING_WEIGHTS


def score_supplier(
    db: Session, supplier_id: int, category: str | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    """Score a supplier based on the 5-dimension model (v2).

    If *weights* is provided it overrides the stored configuration weights.
    """
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        raise ValueError(f"Supplier {supplier_id} not found")

    weights = weights or get_scoring_weights(db)

    # ── 1. Price competitiveness (使用合理史低而非中位价) ─────────────────
    q = db.query(Quote).filter(Quote.supplier_id == supplier_id, Quote.unit_price > 0)
    if category:
        q = q.join(Material).filter(Material.category == category)
    supplier_quotes = q.all()

    price_score = 60.0
    if supplier_quotes:
        deviations = []
        for quote in supplier_quotes:
            mat = db.get(Material, quote.material_id)
            ref = mat.ref_price_reasonable_low if mat else None
            if ref is None and mat:
                ref = mat.ref_price_median   # fallback
            if mat and ref and ref > 0:
                dev = (quote.unit_price - ref) / ref
                deviations.append(dev)
        if deviations:
            avg_dev = float(np.mean(deviations))
            if avg_dev <= -0.10:
                price_score = 100.0
            elif avg_dev <= 0:
                price_score = 80.0 + (0.10 + avg_dev) / 0.10 * 20.0
            elif avg_dev <= 0.10:
                price_score = 60.0 + (0.10 - avg_dev) / 0.10 * 20.0
            elif avg_dev <= 0.20:
                price_score = 40.0 + (0.20 - avg_dev) / 0.10 * 20.0
            else:
                price_score = max(20.0, 40.0 - (avg_dev - 0.20) * 100)

    # ── 2. History cooperation ────────────────────────────────────────────
    win_count = supplier.win_count
    if win_count >= 5:
        history_score = 100.0
    elif win_count >= 3:
        history_score = 80.0
    elif win_count >= 1:
        history_score = 60.0
    else:
        history_score = 40.0

    # ── 3. Quote completeness ─────────────────────────────────────────────
    total_quotes = db.query(func.count(Quote.id)).filter(
        Quote.supplier_id == supplier_id
    ).scalar() or 0
    valid_quotes = db.query(func.count(Quote.id)).filter(
        Quote.supplier_id == supplier_id, Quote.unit_price > 0,
    ).scalar() or 0
    completeness_score = min(100.0, (valid_quotes / total_quotes) * 100) if total_quotes > 0 else 50.0

    # ── 4. Brand compliance (品牌档位命中率) ──────────────────────────────
    brand_score = _compute_brand_score(db, supplier_id, category)

    # ── 5. Commercial terms ───────────────────────────────────────────────
    commercial_score = float(supplier.cooperation_score) if supplier.cooperation_score > 0 else 60.0

    # Keys must match DEFAULT_SCORING_WEIGHTS / SettingsView.vue (long names).
    # Bug-fix 2026-05-21: previously used short keys ("price", "history", ...)
    # which never matched the stored config → user weight changes had zero effect.
    total = (
        price_score * weights.get("price_competitiveness", 0.40)
        + history_score * weights.get("history_cooperation", 0.20)
        + completeness_score * weights.get("quote_completeness", 0.15)
        + brand_score * weights.get("brand_compliance", 0.15)
        + commercial_score * weights.get("commercial_terms", 0.10)
    )

    return {
        "supplier_id": supplier_id,
        "supplier_name": supplier.name,
        "price_score": round(price_score, 1),
        "history_score": round(history_score, 1),
        "completeness_score": round(completeness_score, 1),
        "brand_score": round(brand_score, 1),
        "commercial_score": round(commercial_score, 1),
        "total_score": round(total, 1),
        "weights": weights,
    }


def _compute_brand_score(db: Session, supplier_id: int, category: str | None) -> float:
    """计算品牌档位命中率得分：有档位的品牌数 / 总品牌数 × 100，无数据则返回 70.0。"""
    q = db.query(Quote.brand).filter(
        Quote.supplier_id == supplier_id,
        Quote.brand != "",
        Quote.brand.isnot(None),
    )
    if category:
        q = q.join(Material).filter(Material.category == category)
    brands = list({r[0] for r in q.all() if r[0]})

    if not brands:
        return 70.0

    # 查哪些品牌有档位
    tiered = db.query(BrandTier.brand_name).filter(BrandTier.brand_name.in_(brands)).all()
    tiered_set = {r[0] for r in tiered}
    hit_rate = len(tiered_set) / len(brands)
    return round(hit_rate * 100, 1)


def compare_multiple_suppliers(
    db: Session, supplier_ids: list[int], category: str,
    project_id: int | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    """Compare multiple suppliers for the same category."""
    results = []
    for sid in supplier_ids:
        supplier = db.get(Supplier, sid)
        if not supplier:
            continue

        q = db.query(Quote).join(Material).filter(
            Quote.supplier_id == sid,
            Material.category == category,
            Quote.unit_price > 0,
        )
        if project_id:
            q = q.filter(Quote.project_id == project_id)
        quotes = q.all()

        avg_price = float(np.mean([qt.unit_price for qt in quotes])) if quotes else None
        total_q = db.query(func.count(Quote.id)).filter(Quote.supplier_id == sid).scalar() or 0
        valid_q = db.query(func.count(Quote.id)).filter(
            Quote.supplier_id == sid, Quote.unit_price > 0,
        ).scalar() or 0
        completeness = valid_q / total_q if total_q > 0 else 0.0

        score = score_supplier(db, sid, category, weights=weights)

        results.append({
            "supplier_id": sid,
            "supplier_name": supplier.name,
            "avg_price": round(avg_price, 2) if avg_price else None,
            "quote_count": len(quotes),
            "completeness": round(completeness, 3),
            "score": score,
        })

    return {"category": category, "suppliers": results}
