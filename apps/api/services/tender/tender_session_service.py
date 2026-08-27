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
  - get_current_session_any_status: is_current only (any status) — used by the
    read-only /tender-list/current detail endpoint, which historically surfaces
    the current session regardless of confirmation. The "_any_status" suffix is
    load-bearing: a bare get_current_session() read like a gate and was used as
    one by mistake (bid_export_service, see docs/design/22 §C1) — the longer
    name is deliberately harder to reach for without noticing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.models.tender_list_session import TenderListSession


def _anchor_identity(a: dict) -> tuple[str, str]:
    """(name, spec), stripped — the content key used to carry anchor_uid across
    list revisions. Intentionally NOT seq: rows shift position when earlier
    rows are inserted/removed, and seq-based matching would treat every row
    after the edit point as "changed" even though nothing about them changed.
    """
    return (str(a.get("name") or "").strip(), str(a.get("spec") or "").strip())


def _assign_anchor_uids(anchors_json: list, previous_anchors_json: list | None) -> list:
    """docs/design/42 P1 — give each anchor a stable id that survives a list
    revision, so cross-round trend data (P2) has something to join on.

    A row carries its anchor_uid forward when (name, spec) matches a row in
    the immediately-previous version, exactly once each (first match wins,
    so duplicate name+spec pairs don't all collapse onto one previous uid).
    Anything else — new row, or a row whose name AND spec both changed —
    gets a fresh uid. This is the safe default from docs/design/42 §9: a
    wrong carry-over fabricates a discount figure, a missed carry-over only
    restarts one row's trend, so ambiguous cases resolve to "new".
    """
    prev_by_identity: dict[tuple[str, str], list[str]] = {}
    for pa in (previous_anchors_json or []):
        if not isinstance(pa, dict):
            continue
        uid = str(pa.get("anchor_uid") or "")
        if not uid:
            continue
        prev_by_identity.setdefault(_anchor_identity(pa), []).append(uid)

    out: list = []
    for a in anchors_json:
        if not isinstance(a, dict):
            out.append(a)
            continue
        candidates = prev_by_identity.get(_anchor_identity(a)) or []
        uid = candidates.pop(0) if candidates else uuid.uuid4().hex
        out.append({**a, "anchor_uid": uid})
    return out


def get_current_confirmed_session(db: Session, project_id: int, category: str):
    """Return the current confirmed TenderListSession, or None.

    Requires BOTH is_current=True AND status='confirmed'. Callers that only
    checked is_current may silently operate on unconfirmed sessions — this
    helper enforces the full gate.
    """
    return db.scalar(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        )
    )


def get_finalization_snapshot(db: Session, project_id: int, category: str):
    """Return the most recent finalized AlignmentFinalization, or None."""
    from apps.api.models.alignment_finalization import AlignmentFinalization

    return db.scalar(
        select(AlignmentFinalization).where(
            AlignmentFinalization.project_id == project_id,
            AlignmentFinalization.category == category,
            AlignmentFinalization.status == "finalized",
        )
        .order_by(AlignmentFinalization.created_at.desc())
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
    db.execute(update(TenderListSession).where(
        TenderListSession.project_id == project_id,
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    ).values(is_current=False, superseded_at=datetime.now(timezone.utc)))

    last = db.scalar(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
        )
        .order_by(TenderListSession.version.desc())
    )
    new_version = (last.version + 1) if last else 1
    anchors_json = _assign_anchor_uids(anchors_json, last.anchors_json if last else None)

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
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(session)
    return session


def list_current_sessions(db: Session, project_id: int) -> list[TenderListSession]:
    """All current (is_current) sessions for a project, ordered by id asc."""
    return db.scalars(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
        )
        .order_by(TenderListSession.id.asc())
    ).all()


def get_current_session_any_status(
    db: Session, category: str, project_id: int | None = None
):
    """Current (is_current, any status) session for a category, or None.

    Read-only detail lookup. NOT a compare/confirm gate — use
    get_current_confirmed_session for that.
    """
    stmt = select(TenderListSession).where(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        stmt = stmt.where(TenderListSession.project_id == project_id)
    return db.scalar(stmt)


def list_versions(
    db: Session, category: str, project_id: int | None = None
) -> list[TenderListSession]:
    """All versions for a category (newest first), optionally project-scoped."""
    stmt = select(TenderListSession).where(TenderListSession.category == category)
    if project_id is not None:
        stmt = stmt.where(TenderListSession.project_id == project_id)
    return db.scalars(stmt.order_by(TenderListSession.version.desc())).all()


def deactivate_current(
    db: Session, category: str, project_id: int | None = None
) -> int:
    """Mark the current version is_current=False (keep history). Commits.

    Returns the number of rows updated.
    """
    stmt = update(TenderListSession).where(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        stmt = stmt.where(TenderListSession.project_id == project_id)
    updated = db.execute(stmt.values(is_current=False, superseded_at=datetime.now(timezone.utc))).rowcount
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
    return db.scalar(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
            TenderListSession.status == "confirmed",
        )
        .order_by(TenderListSession.id.desc())
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
    return db.scalar(
        select(TenderListSession).where(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
            TenderListSession.is_current.is_(True),
        )
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
