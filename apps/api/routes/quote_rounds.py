"""QuoteRound CRUD — docs/design/42 P0.

A round is one complete, independently-comparable collection of quotes for
(project, category). See apps/api/services/tender/quote_round_service.py for
the invariants (at most one open round, is_final_basis is explicit-only).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models import Project, QuoteRound
from apps.api.schemas import QuoteRoundCreate, QuoteRoundOut, QuoteRoundUpdate
from apps.api.services.tender import quote_round_service as svc

router = APIRouter(prefix="/api/projects/{project_id}/quote-rounds", tags=["quote-rounds"])


def _get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("", response_model=list[QuoteRoundOut])
def list_quote_rounds(
    project_id: int,
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)
    return svc.list_rounds(db, project_id, category)


@router.get("/current", response_model=QuoteRoundOut | None)
def get_current_round(
    project_id: int,
    category: str = Query(""),
    db: Session = Depends(get_db),
):
    """The open round for this scope, or null if none has been opened yet.

    Read-only — does NOT auto-create. Uploads use the auto-creating path in
    quote_confirmation_service; this endpoint is for the frontend round
    selector to know whether to show "no round yet" or an actual round.
    """
    _get_project_or_404(db, project_id)
    return svc.get_open_round(db, project_id, category)


@router.post("", response_model=QuoteRoundOut, status_code=201)
def create_quote_round(
    project_id: int,
    body: QuoteRoundCreate,
    db: Session = Depends(get_db),
):
    """Explicitly open a new round. Closes whatever round was open before it
    in the same (project, category) scope — see quote_round_service.create_round.
    """
    _get_project_or_404(db, project_id)
    try:
        round_ = svc.create_round(
            db, project_id, body.category, name=body.name,
            stage=body.stage, remark=body.remark,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return round_


@router.patch("/{round_id}", response_model=QuoteRoundOut)
def update_quote_round(
    project_id: int,
    round_id: int,
    body: QuoteRoundUpdate,
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)
    round_ = db.get(QuoteRound, round_id)
    if not round_ or round_.project_id != project_id:
        raise HTTPException(404, "QuoteRound not found")

    if body.name is not None:
        round_ = svc.rename_round(db, round_id, body.name)
    if body.status is not None:
        if body.status == "open":
            round_ = svc.reopen_round(db, round_id)
        elif body.status == "closed":
            round_ = svc.close_round(db, round_id)
        else:
            raise HTTPException(422, f"Unknown status: {body.status!r} (expected open/closed)")
    if body.is_final_basis is not None:
        round_ = svc.set_final_basis(db, round_id, body.is_final_basis)

    return round_
