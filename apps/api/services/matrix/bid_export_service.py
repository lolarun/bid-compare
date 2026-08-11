"""BidExportService — scope resolution and matrix computation for export routes.

Extracted from routes/export.py so the route handler is responsible only for
serialization (Excel formatting), not for business logic.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.errors import ConflictError, ValidationError
from apps.api.services.matrix.bid_matrix import build_anchor_matrix
from apps.api.services.tender.tender_session_service import (
    get_current_confirmed_session,
    get_finalization_snapshot,
)


def get_bid_matrix_for_export(
    db: Session,
    project_id: int,
    category: str,
    supplier_ids: list[int],
) -> dict:
    """Resolve scope and compute the comparison matrix for Excel export.

    Requires a current TenderListSession for the project+category; raises 400
    if none exists and 409 if submission scope is inconsistent (submission
    records exist but used_submission_ids was never written).

    Returns the raw matrix dict from build_anchor_matrix — the caller
    (export route) is responsible for Excel serialization.
    """
    from apps.api.services.tender.tender_list import rebuild_anchors
    from apps.api.models.bid_submission import BidSubmission as _BS

    session = get_current_confirmed_session(db, project_id, category)
    if not session or not session.anchors_json:
        raise ValidationError(
            f"项目 {project_id} / 品类 {category} 尚无已确认采购清单（TenderListSession）。"
            "请先完成采购清单上传和确认步骤后再导出。"
        )

    # 硬闸门：used_submission_ids 必须已写入（与 /bid-matrix 保持一致）
    used_sub_ids = list(session.used_submission_ids or [])
    if not used_sub_ids:
        any_active = db.scalar(
            select(_BS.id).where(
                _BS.project_id == project_id,
                _BS.status.notin_(("rejected", "superseded")),
            )
        )
        if any_active:
            raise ConflictError(
                "报价确认异常：项目存在 BidSubmission 但当前会话 used_submission_ids 为空。"
                "请重新执行「校对入库」→「对齐核查」后再导出矩阵。",
            )

    anchors = rebuild_anchors(session)

    fin = get_finalization_snapshot(db, project_id, category)
    allowed_group_ids = set(fin.group_ids_json) if fin and fin.group_ids_json else None

    return build_anchor_matrix(
        db,
        anchors=anchors,
        tender_list_session_id=session.id,
        used_submission_ids=used_sub_ids,
        supplier_ids=supplier_ids,
        project_id=project_id,
        category=category,
        allowed_group_ids=allowed_group_ids,
    )
