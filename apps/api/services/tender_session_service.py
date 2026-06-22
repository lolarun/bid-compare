"""TenderSessionService — authoritative TenderListSession lifecycle + queries.

P1-1 first slice (docs/design/12 §10.1-10.3): consolidates session resolution,
creation/versioning, listing and deactivation that were previously written as
inline TenderListSession queries scattered across routes/analysis.py. Routes
must call these functions instead of hand-rolling session queries, so every
entry point shares one definition of "current / confirmed / version".

Behavior is preserved verbatim from the original inline route code; this is a
move + naming consolidation, not a semantic change.

Naming distinction (intentional, both kept):
  - get_current_confirmed_session: is_current AND status='confirmed' — the gate
    every compare/match/matrix entry point must use.
  - get_current_session: is_current only (any status) — used by the read-only
    /tender-list/current detail endpoint, which historically surfaces the
    current session regardless of confirmation. Do NOT use it as a compare gate.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from apps.api.models.tender_list_session import TenderListSession


def get_current_confirmed_session(db: Session, project_id: int, category: str):
    """Return the current confirmed TenderListSession, or None.

    Requires BOTH is_current=True AND status='confirmed'. Callers that only
    checked is_current may silently operate on unconfirmed sessions — this
    helper enforces the full gate.
    """
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


def save_session(
    db: Session,
    project_id: int,
    category: str,
    file_name: str,
    anchors_json: list,
    confirmed_by,
    source_type: str = "excel",
    brand_requirement=None,
    supplier_brand_map=None,
) -> TenderListSession:
    """Create a new confirmed TenderListSession version for (project, category).

    Supersedes the existing current version (is_current=False + superseded_at)
    and bumps the version number. Returns the new session WITHOUT committing —
    the caller owns the transaction boundary.
    """
    db.query(TenderListSession).filter(
        TenderListSession.project_id == project_id,
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    ).update({"is_current": False, "superseded_at": datetime.utcnow()})

    last = (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
        )
        .order_by(TenderListSession.version.desc())
        .first()
    )
    new_version = (last.version + 1) if last else 1

    session = TenderListSession(
        project_id=project_id,
        category=category,
        file_name=file_name,
        source_type=source_type,
        anchors_total=len(anchors_json),
        anchors_json=anchors_json,
        brand_requirement=brand_requirement,
        supplier_brand_map=supplier_brand_map,
        version=new_version,
        is_current=True,
        status="confirmed",
        confirmed_by=confirmed_by or None,
        confirmed_at=datetime.utcnow(),
    )
    db.add(session)
    return session


def list_current_sessions(db: Session, project_id: int) -> list[TenderListSession]:
    """All current (is_current) sessions for a project, ordered by id asc."""
    return (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
        )
        .order_by(TenderListSession.id.asc())
        .all()
    )


def get_current_session(
    db: Session, category: str, project_id: int | None = None
):
    """Current (is_current, any status) session for a category, or None.

    Read-only detail lookup. NOT a compare/confirm gate — use
    get_current_confirmed_session for that.
    """
    q = db.query(TenderListSession).filter(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    return q.first()


def list_versions(
    db: Session, category: str, project_id: int | None = None
) -> list[TenderListSession]:
    """All versions for a category (newest first), optionally project-scoped."""
    q = db.query(TenderListSession).filter(TenderListSession.category == category)
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    return q.order_by(TenderListSession.version.desc()).all()


def deactivate_current(
    db: Session, category: str, project_id: int | None = None
) -> int:
    """Mark the current version is_current=False (keep history). Commits.

    Returns the number of rows updated.
    """
    q = db.query(TenderListSession).filter(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    updated = q.update({"is_current": False, "superseded_at": datetime.utcnow()})
    db.commit()
    return updated


# ── Second-slice helpers (replaces remaining inline TLS queries in routes) ───


def get_any_current_confirmed_session(db: Session, project_id: int):
    """Return the most-recent confirmed current session for the project, any category.

    Used as a fallback when the caller has not specified a category: e.g. the
    match route finds whatever confirmed session the project currently has.
    Prefer get_current_confirmed_session (category-scoped) when the category
    is known.
    """
    return (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        )
        .order_by(TenderListSession.id.desc())
        .first()
    )


def get_session_for_fill(
    db: Session,
    project_id: int,
    category: str,
    tls_id: int | None = None,
) -> TenderListSession | None:
    """Resolve session for the LLM-fill route.

    When tls_id is provided, fetch by primary key (caller's explicit choice).
    Otherwise fall back to the current is_current session for project+category
    (any status — fill can work on unconfirmed sessions too).
    """
    if tls_id is not None:
        return db.get(TenderListSession, tls_id)
    return (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
            TenderListSession.is_current.is_(True),
        )
        .first()
    )


def record_submission_scope(
    db: Session,
    tls_id: int,
    sub_ids: list[int] | None,
    supplier_ids: list[int] | None,
) -> None:
    """Persist the resolved submission scope onto TenderListSession after a match run.

    Writes used_submission_ids (when sub_ids provided) and/or
    confirmed_supplier_ids (when supplier_ids provided) then commits.
    Callers that cannot determine sub_ids may still pass None to skip that field.
    """
    tls = db.get(TenderListSession, tls_id)
    if tls is None:
        return
    if supplier_ids is not None:
        tls.confirmed_supplier_ids = sorted(set(supplier_ids))
    if sub_ids is not None:
        tls.used_submission_ids = sorted(sub_ids)
    db.commit()
