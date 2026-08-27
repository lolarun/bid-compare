"""quote_round_service.py — QuoteRound lifecycle (docs/design/42 P0).

Mirrors the shape of tender_session_service.py: one "current" thing per
(project_id, category), superseded rather than deleted.

Invariant this module enforces: **at most one `open` round** per
(project_id, category). Opening a new round implicitly closes whatever round
was open before it — starting round 2 means round 1's collection is done.
This mirrors TenderListSession.is_current, and is what lets intake default to
"the current round" without the caller having to resolve it explicitly.

`is_final_basis` is the opposite kind of invariant (docs/design/42 §8 D3):
it is never touched by opening/closing a round, only by an explicit
`set_final_basis` call, and at most one round per (project_id, category) may
carry it — setting it on one round clears it on the others in the same scope.
A round with `is_final_basis=False` is not an error state; it is the default,
and downstream official-result consumers (evaluation/export/recommendation —
not yet wired, P2) must refuse to run against a scope with none set rather
than guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.models.quote_round import QuoteRound, STAGE_FORMAL, STAGES, STATUS_CLOSED, STATUS_OPEN


def get_open_round(db: Session, project_id: int, category: str) -> QuoteRound | None:
    """The current open round for (project_id, category), or None."""
    return db.scalar(
        select(QuoteRound).where(
            QuoteRound.project_id == project_id,
            QuoteRound.category == category,
            QuoteRound.status == STATUS_OPEN,
        )
    )


def get_or_open_round(
    db: Session, project_id: int, category: str, created_by: str | None = None,
) -> QuoteRound:
    """Return the current open round, auto-creating round 1 if none exists yet.

    This is the default every upload attaches to — a project that never
    explicitly created a round still gets one, transparently, the first time
    a quote is confirmed for it.
    """
    existing = get_open_round(db, project_id, category)
    if existing:
        return existing
    return create_round(db, project_id, category, created_by=created_by)


def list_rounds(db: Session, project_id: int, category: str | None = None) -> list[QuoteRound]:
    """All rounds for a project, newest first. Optionally category-scoped."""
    stmt = select(QuoteRound).where(QuoteRound.project_id == project_id)
    if category is not None:
        stmt = stmt.where(QuoteRound.category == category)
    return db.scalars(stmt.order_by(QuoteRound.category.asc(), QuoteRound.seq.desc())).all()


def create_round(
    db: Session,
    project_id: int,
    category: str,
    name: str = "",
    stage: str = STAGE_FORMAL,
    created_by: str | None = None,
    remark: str = "",
) -> QuoteRound:
    """Explicitly open a new round, closing whatever round was open before it.

    Does NOT commit — caller owns the transaction boundary (same convention
    as tender_session_service.save_session).
    """
    if stage not in STAGES:
        raise ValueError(f"Unknown round stage: {stage!r}")

    now = datetime.now(timezone.utc)
    db.execute(update(QuoteRound).where(
        QuoteRound.project_id == project_id,
        QuoteRound.category == category,
        QuoteRound.status == STATUS_OPEN,
    ).values(status=STATUS_CLOSED, closed_at=now))

    last = db.scalar(
        select(QuoteRound).where(
            QuoteRound.project_id == project_id,
            QuoteRound.category == category,
        ).order_by(QuoteRound.seq.desc())
    )
    seq = (last.seq + 1) if last else 1

    round_ = QuoteRound(
        project_id=project_id,
        category=category,
        seq=seq,
        name=name or f"第{seq}轮",
        stage=stage,
        status=STATUS_OPEN,
        is_final_basis=False,
        created_by=created_by or None,
        remark=remark,
        opened_at=now,
    )
    db.add(round_)
    db.flush()
    return round_


def rename_round(db: Session, round_id: int, name: str) -> QuoteRound | None:
    """User-authored label (docs/design/42 req. 3). Commits."""
    round_ = db.get(QuoteRound, round_id)
    if round_ is None:
        return None
    round_.name = name
    db.commit()
    return round_


def close_round(db: Session, round_id: int) -> QuoteRound | None:
    """Mark a round closed. Does not touch is_final_basis. Commits."""
    round_ = db.get(QuoteRound, round_id)
    if round_ is None:
        return None
    round_.status = STATUS_CLOSED
    round_.closed_at = datetime.now(timezone.utc)
    db.commit()
    return round_


def reopen_round(db: Session, round_id: int) -> QuoteRound | None:
    """Reopen a closed round, closing whatever round is currently open in its
    (project_id, category) scope. Commits.
    """
    round_ = db.get(QuoteRound, round_id)
    if round_ is None:
        return None
    db.execute(update(QuoteRound).where(
        QuoteRound.project_id == round_.project_id,
        QuoteRound.category == round_.category,
        QuoteRound.status == STATUS_OPEN,
        QuoteRound.id != round_.id,
    ).values(status=STATUS_CLOSED, closed_at=datetime.now(timezone.utc)))
    round_.status = STATUS_OPEN
    round_.closed_at = None
    db.commit()
    return round_


def set_final_basis(db: Session, round_id: int, is_final_basis: bool) -> QuoteRound | None:
    """Explicitly flag (or unflag) a round as the official basis (docs/design/42
    §8 D3). Setting True clears the flag on every other round in the same
    (project_id, category) scope — at most one basis round at a time, so
    "which round is official" never needs guessing downstream. Commits.
    """
    round_ = db.get(QuoteRound, round_id)
    if round_ is None:
        return None
    if is_final_basis:
        db.execute(update(QuoteRound).where(
            QuoteRound.project_id == round_.project_id,
            QuoteRound.category == round_.category,
            QuoteRound.id != round_.id,
        ).values(is_final_basis=False))
    round_.is_final_basis = is_final_basis
    db.commit()
    return round_


def get_final_basis_round(db: Session, project_id: int, category: str) -> QuoteRound | None:
    """The round official results must be computed from, or None if the user
    has not designated one yet (docs/design/42 §8 D3 — never auto-promoted).
    """
    return db.scalar(
        select(QuoteRound).where(
            QuoteRound.project_id == project_id,
            QuoteRound.category == category,
            QuoteRound.is_final_basis.is_(True),
        )
    )


def record_round_scope(
    db: Session,
    round_id: int,
    sub_ids: list[int] | None,
    supplier_ids: list[int] | None,
    tender_list_session_id: int | None = None,
) -> None:
    """Persist the resolved comparison scope onto QuoteRound after a match run.

    Mirrors tender_session_service.record_submission_scope, moved onto the
    round per docs/design/42 §3.1 — a later round must never overwrite an
    earlier round's scope, which writing onto TenderListSession (shared
    across all rounds of a (project, category)) could not guarantee. Callers
    that cannot determine one of the two may pass None to skip that field.

    tender_list_session_id: which confirmed list version this round matched
    against — provenance, not identity (docs/design/42 §4). Without it,
    round_trend has no way to rebuild this round's anchors and must skip it
    (see round_trend.compute_round_trend's `skipped_rounds`). The only other
    writer of this field is migration 0009's synthesized round 1 backfill;
    every real round created after P0 needs this call to ever be queryable.

    Commits.
    """
    round_ = db.get(QuoteRound, round_id)
    if round_ is None:
        return
    if supplier_ids is not None:
        round_.confirmed_supplier_ids = sorted(set(supplier_ids))
    if sub_ids is not None:
        round_.used_submission_ids = sorted(sub_ids)
    if tender_list_session_id is not None:
        round_.tender_list_session_id = tender_list_session_id
    db.commit()
