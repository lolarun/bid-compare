"""Single business result for the invitation recommendation flow.

The UI may ask for a preview and later save selected suppliers.  Both paths
must use the same deterministic evidence rather than trusting client scores or
re-running slightly different route logic.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from apps.api.services.brand_recommend import recommend_brands
from apps.api.services.supplier_recommend import recommend_suppliers

MAX_SUPPLIER_RECOMMENDATIONS = 12


def build_invitation_recommendation(
    db: Session,
    tender_items: list[dict[str, Any]],
    *,
    top_n: int,
    project_id: int | None = None,
    brand_requirements: list[str] | None = None,
) -> dict[str, Any]:
    """Return reviewable recommendations and explicit evidence gaps.

    This is intentionally deterministic.  A future LLM may normalize an
    uploaded document before this call, but it must not alter the ranking.
    """
    safe_top_n = max(1, top_n)
    brand_recommendations, categories = recommend_brands(
        db, tender_items, top_n=safe_top_n
    )
    supplier_recommendations, supplier_total = recommend_suppliers(
        db,
        tender_items,
        top_n=min(safe_top_n, MAX_SUPPLIER_RECOMMENDATIONS),
        project_id=project_id,
        brand_requirements=brand_requirements or None,
    )

    data_gaps: list[str] = []
    if not categories:
        data_gaps.append("未能识别采购品类，无法按历史证据召回供应商。")
    elif not supplier_recommendations:
        data_gaps.append("未找到有历史报价证据的供应商；可补充供应商名录或历史报价文件。")
    elif all(
        rec.get("reason", {}).get("history_count", 0) == 0
        for rec in supplier_recommendations
    ):
        data_gaps.append("当前候选来自供应商名录，暂无相关品类的历史报价证据，请人工确认。")

    if brand_requirements:
        covered = {
            brand
            for rec in supplier_recommendations
            for brand in rec.get("reason", {}).get("brands", [])
        }
        missing = [brand for brand in brand_requirements if brand not in covered]
        if missing:
            data_gaps.append(f"以下指定品牌暂无供应商历史报价证据：{'、'.join(missing)}。")

    return {
        "categories": categories,
        "brand_recommendations": brand_recommendations,
        "supplier_recommendations": supplier_recommendations,
        "total_supplier_candidates": supplier_total,
        "data_gaps": data_gaps,
    }
