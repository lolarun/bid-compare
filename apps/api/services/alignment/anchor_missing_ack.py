"""AnchorMissingAck 服务（docs/design/23）。

复核者对"这个锚点×投标确实无报价，符合预期"的显式确认——纯审计/UI 抑制
标记。**不创建报价、不改 cell_status、不进评标总价**（design/23 §6 的安全
论证；build_anchor_review_matrix 只在 cell_status 已经是 missing 时才查这
张表，查到与否都不改变 cell_status 本身）。

只覆盖 submission 模式（§7 权威列身份）；legacy supplier_ids 模式的复核
矩阵不接这个功能，与本仓库其余新功能只做 submission 优先的方向一致。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.errors import ConflictError
from apps.api.models.anchor_missing_ack import AnchorMissingAck


def set_missing_ack(
    db: Session,
    project_id: int,
    category: str,
    anchor_seq: str,
    submission_id: int,
    acked: bool,
    reason: str = "",
    acked_by: str = "",
) -> AnchorMissingAck | None:
    """按 (session, anchor_seq, submission_id) 幂等置位。**不提交事务**——
    和 anchor_review_item_confirm 一样，提交交给路由，与同一请求里的
    write_domain_event 落在同一个事务里，不拆成两次 commit。

    acked=True：不存在则创建，已存在则刷新 reason/acked_by/created_at（重复
    确认不报错，也不会插出第二行——数据库唯一约束兜底，这里的显式查找是为了
    在冲突之前就拿到既有行，给出"更新"而不是"唯一约束报错后兜底"的正常路径）。
    acked=False：不存在则视为成功（已经是未确认状态），存在则删除。

    调用方必须自己解析 tender_list_session_id 并处理"无已确认采购清单"——
    这里不重复 build_anchor_review_matrix 已经做过的那层会话解析，避免
    两处判定漂移。
    """
    from apps.api.services.tender.tender_session_service import (
        get_current_confirmed_session,
    )

    session = get_current_confirmed_session(db, project_id, category)
    if not session:
        raise ConflictError(f"No current TenderListSession for project {project_id} / {category}")

    existing = db.scalar(
        select(AnchorMissingAck).where(
            AnchorMissingAck.tender_list_session_id == session.id,
            AnchorMissingAck.anchor_seq == anchor_seq,
            AnchorMissingAck.submission_id == submission_id,
        )
    )

    if not acked:
        if existing:
            db.delete(existing)
            db.flush()
        return None

    if existing:
        existing.reason = reason
        existing.acked_by = acked_by
        db.flush()
        return existing

    row = AnchorMissingAck(
        project_id=project_id,
        category=category,
        tender_list_session_id=session.id,
        anchor_seq=anchor_seq,
        submission_id=submission_id,
        reason=reason,
        acked_by=acked_by,
    )
    db.add(row)
    db.flush()
    return row


def get_missing_ack_set(db: Session, tender_list_session_id: int) -> set[tuple[str, int]]:
    """一次查询取出某会话下已确认的 (anchor_seq, submission_id) 集合。

    build_anchor_review_matrix 每次构建矩阵调用一次，供逐格 O(1) 查找。
    """
    rows = db.execute(
        select(AnchorMissingAck.anchor_seq, AnchorMissingAck.submission_id).where(
            AnchorMissingAck.tender_list_session_id == tender_list_session_id,
        )
    ).all()
    return {(seq, sid) for seq, sid in rows}
