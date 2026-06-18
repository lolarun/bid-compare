"""Shared utility: resolve the active BidSubmission set for a project+category.

All endpoints that need to identify which BidSubmission rows to use
(match, anchor-review, residue, LLM-fill) MUST call this function instead
of writing their own submission queries, so they all operate on the same batch.

Return type: dict[submission_id → BidSubmission]  (keyed by BidSubmission.id)
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from apps.api.models.bid_submission import BidQuoteLine, BidSubmission


def resolve_active_submissions(
    db: Session,
    project_id: int,
    category: str,
    supplier_ids: list[int] | None = None,
    submission_ids: list[int] | None = None,
) -> dict[int, BidSubmission]:
    """Return {submission_id → BidSubmission} for active submissions.

    Filters (OR-combined when both given):
    - supplier_ids: include submissions with supplier_id in list
    - submission_ids: include submissions with id in list
    - Always: project_id, category, status != rejected, has BQL rows for category

    Rules:
    - Excludes submissions with status='rejected'.
    - Only includes submissions that have at least one BidQuoteLine for `category`.
    """
    q = (
        db.query(BidSubmission)
        .join(BidQuoteLine, BidSubmission.id == BidQuoteLine.submission_id)
        .filter(
            BidSubmission.project_id == project_id,
            BidSubmission.status != "rejected",
            BidQuoteLine.category == category,
        )
        .distinct()
    )

    if supplier_ids and submission_ids:
        q = q.filter(
            or_(
                BidSubmission.supplier_id.in_(supplier_ids),
                BidSubmission.id.in_(submission_ids),
            )
        )
    elif supplier_ids:
        q = q.filter(BidSubmission.supplier_id.in_(supplier_ids))
    elif submission_ids:
        q = q.filter(BidSubmission.id.in_(submission_ids))

    result: dict[int, BidSubmission] = {}
    for sub in q.order_by(BidSubmission.id.asc()).all():
        if sub.id not in result:
            result[sub.id] = sub
    return result
