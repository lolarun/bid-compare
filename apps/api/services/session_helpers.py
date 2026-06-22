"""Shared helpers for tender session and finalization snapshot queries.

Single source of truth for:
  - get_current_confirmed_session: is_current AND status=confirmed
  - get_finalization_snapshot: latest finalized AlignmentFinalization

All callers MUST use these helpers instead of writing inline queries.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


def get_current_confirmed_session(db: Session, project_id: int, category: str):
    """Return the current confirmed TenderListSession, or None.

    Requires BOTH is_current=True AND status='confirmed'. Callers that only
    checked is_current may silently operate on unconfirmed sessions — this
    helper enforces the full gate.
    """
    from apps.api.models.tender_list_session import TenderListSession
    return (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        )
        .first()
    )


def get_finalization_snapshot(db: Session, project_id: int, category: str):
    """Return the most recent finalized AlignmentFinalization, or None."""
    from apps.api.models.alignment_finalization import AlignmentFinalization
    return (
        db.query(AlignmentFinalization)
        .filter(
            AlignmentFinalization.project_id == project_id,
            AlignmentFinalization.category == category,
            AlignmentFinalization.status == "finalized",
        )
        .order_by(AlignmentFinalization.created_at.desc())
        .first()
    )
