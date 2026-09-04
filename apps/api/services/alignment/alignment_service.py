"""AlignmentService — alignment finalization authority.

Extracted from routes/analysis.py so the business rules for locking an
alignment snapshot live in a single testable service, not inline in the route.

The route (anchor_review_finalize) delegates here and handles HTTP mapping only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func as _func
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.errors import ConflictError, ValidationError
from apps.api.models.alignment_finalization import AlignmentFinalization
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.services.audit import EVENT_ALIGNMENT_FINALIZE, write_domain_event


@dataclass
class FinalizationResult:
    id: int
    group_ids_count: int
    pending_at_finalize: int
    forced: bool


def finalize_alignment(
    db: Session,
    project_id: int,
    category: str,
    force: bool = False,
    reason: str = "",
    finalized_by: str = "",
) -> FinalizationResult:
    """Create an AlignmentFinalization locking the current confirmed group snapshot.

    Raises ValidationError (→400) if force=True without reason.
    Raises ConflictError (→409) if there are pending items or valve_type_conflict
    align items and force=False.

    Commits the new AlignmentFinalization before returning.
    """
    if force and not reason:
        raise ValidationError("force=True 时必须提供 reason 字段")

    # Gate 1: item-level pending check
    pending_count = db.scalar(
        select(_func.count(BidAlignmentItem.id))
        .join(BidAlignmentGroup, BidAlignmentItem.group_id == BidAlignmentGroup.id)
        .where(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentItem.action == "pending",
        )
    ) or 0
    if pending_count > 0 and not force:
        raise ConflictError(
            f"仍有 {pending_count} 条 item 处于 pending 状态未处理。"
            "请先逐条确认，或使用 force=true 强制完成（需提供原因）。",
        )

    # Gate 2: valve_type_conflict align items
    fp_align_count = db.scalar(
        select(_func.count(BidAlignmentItem.id))
        .join(BidAlignmentGroup, BidAlignmentItem.group_id == BidAlignmentGroup.id)
        .where(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
            BidAlignmentItem.action == "align",
            BidAlignmentItem.spec_note.like("%valve_type_conflict%"),
        )
    ) or 0
    if fp_align_count > 0 and not force:
        raise ConflictError(
            f"存在 {fp_align_count} 条 align item 含阀型冲突标记，拒绝 finalize。"
            "请重新运行 LLM 填表或使用 force=true 强制完成（需提供原因）。",
        )

    # Lock current confirmed group ID snapshot
    confirmed_groups = db.scalars(
        select(BidAlignmentGroup).where(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
        )
    ).all()
    group_ids = [g.id for g in confirmed_groups]

    fin = AlignmentFinalization(
        project_id=project_id,
        category=category,
        group_ids_json=group_ids,
        status="finalized",
        pending_at_finalize=pending_count,
        finalized_by=finalized_by or None,
        finalized_at=datetime.now(UTC),
        forced=force,
        force_reason=reason if force else None,
    )
    db.add(fin)
    db.flush()  # get fin.id before committing
    write_domain_event(
        db, user=finalized_by or "system", event_type=EVENT_ALIGNMENT_FINALIZE,
        identity={"project_id": project_id, "finalization_id": fin.id},
        after={"category": category, "group_ids_count": len(group_ids), "forced": force},
        meta={"pending_at_finalize": pending_count, "reason": reason if force else ""},
    )
    db.commit()
    db.refresh(fin)

    return FinalizationResult(
        id=fin.id,
        group_ids_count=len(group_ids),
        pending_at_finalize=pending_count,
        forced=force,
    )
