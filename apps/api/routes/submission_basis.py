"""份级口径（可比性基准）读写 + 轮内一致性体检 —— P1。

设计见 `.claude/plans/comparability-basis-dimensions.md`。这层只做搬运与校验，
判定逻辑全在 `services/matrix/basis_consistency.py`（确定性、无模型调用）。

**这里没有"折算"接口，将来也不该有。** 把「不含安装」折成含安装、按铜价换算
总价、给付款条件折现，都需要投标方没给的数；系统能做的只有"标出不可比"。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.security import get_current_user
from apps.api.models import BidSubmission, QuoteRound
from apps.api.models.submission_basis import (
    DIMENSIONS,
    STATUS_CONFIRMED,
    STATUSES,
    SubmissionBasis,
)
from apps.api.services.matrix.basis_consistency import check_round_basis, upsert_basis

router = APIRouter(prefix="/api", tags=["submission-basis"])


class BasisOut(BaseModel):
    submission_id: int
    dim: str
    status: str
    value: dict | None = None
    #: 原文永远随值一起返回——界面不许只显示归一值。归一表出错时，原文是
    #: 唯一能让人看出来的东西。
    raw_text: str = ""
    source_ref: dict | None = None
    extracted_by: str = ""
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


class BasisUpsertIn(BaseModel):
    status: str = Field(..., description="extracted / not_present / extraction_failed / confirmed")
    value: dict | None = None
    raw_text: str = ""
    source_ref: dict | None = None
    extracted_by: str = ""


def _get_submission_or_404(db: Session, submission_id: int) -> BidSubmission:
    sub = db.get(BidSubmission, submission_id)
    if not sub:
        raise HTTPException(404, "Submission not found")
    return sub


@router.get("/submissions/{submission_id}/basis", response_model=list[BasisOut])
def list_submission_basis(submission_id: int, db: Session = Depends(get_db)):
    """一份报价的全部口径取值。**没有的维度不补空行**——"库里没有这条记录"和
    "抽取判定原文里没有声明(not_present)"是两回事，补空行会把前者伪装成后者。"""
    _get_submission_or_404(db, submission_id)
    rows = db.scalars(
        select(SubmissionBasis)
        .where(SubmissionBasis.submission_id == submission_id)
        .order_by(SubmissionBasis.dim)
    ).all()
    return rows


@router.put("/submissions/{submission_id}/basis/{dim}", response_model=BasisOut)
def put_submission_basis(
    submission_id: int,
    dim: str,
    payload: BasisUpsertIn,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """写入/修正一个维度的取值。人工确认走这里（status=confirmed）。

    确认人取当前登录用户；这条路径本身不做角色门禁——用户 2026-09-03 决策 3
    明确「接受口径差异」不限角色、只留审计，确认口径值同理。
    """
    _get_submission_or_404(db, submission_id)
    if dim not in DIMENSIONS:
        raise HTTPException(400, f"Unknown basis dimension: {dim}")
    if payload.status not in STATUSES:
        raise HTTPException(400, f"Unknown basis status: {payload.status}")
    # confirmed 必须带值，除非它表达的就是"原文里没有"——那是 not_present 的活。
    if payload.status == STATUS_CONFIRMED and payload.value is None:
        raise HTTPException(
            400,
            "confirmed 状态必须带 value；若原文里确实没有该维度声明，请用 not_present",
        )

    row = upsert_basis(
        db, submission_id, dim,
        status=payload.status,
        value=payload.value,
        raw_text=payload.raw_text,
        source_ref=payload.source_ref,
        extracted_by=payload.extracted_by,
        # 审计要记真人：确认人取登录用户，不写死。
        confirmed_by=(
            str(current_user.get("sub") or "") if payload.status == STATUS_CONFIRMED else None
        ),
    )
    db.commit()
    db.refresh(row)
    return row


class BasisCheckOut(BaseModel):
    comparable: bool
    conflicts: list[dict]
    unresolved: list[dict]


@router.get("/quote-rounds/{round_id}/basis-check", response_model=BasisCheckOut)
def check_round(round_id: int, db: Session = Depends(get_db)):
    """一轮的口径体检。`comparable=false` 时调用方**不得**出总价排名。

    参与体检的是本轮**已入库**的报价（`round_id` 命中的 BidSubmission）；
    列身份是 submission_id 不是 supplier_id——同一供应商可以有多份报价。
    """
    rnd = db.get(QuoteRound, round_id)
    if not rnd:
        raise HTTPException(404, "Round not found")

    subs = db.scalars(
        select(BidSubmission).where(BidSubmission.round_id == round_id)
    ).all()
    pairs = [(s.id, s.supplier_raw_name or f"#{s.id}") for s in subs]
    return check_round_basis(db, pairs).as_dict()
