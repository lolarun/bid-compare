"""AnchorMissingAck — 复核者对"该锚点×投标无报价"的显式确认。

docs/design/23：missing 单元格没有任何 BidAlignmentItem（没人对它做过匹配），
而 BidAlignmentItem 的 CHECK 约束要求 quote_id/bid_quote_line_id 二选一非空，
"确认为空报价"这个事实装不进那张表。这里单开一张表——纯审计/UI 抑制标记，
不创建报价、不改 cell_status、不影响评标总价（design/23 §6）。

按 tender_list_session_id 隔离：换一版采购清单（锚点重新编号）不会静默带
着上一版的确认状态。
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint, Index

from apps.api.core.database import Base
from apps.api.models._base import _now


class AnchorMissingAck(Base):
    """复核者已确认：这个 anchor_seq × submission_id 组合无报价，符合预期。"""

    __tablename__ = "anchor_missing_acks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    # 索引由 __table_args__ 里显式命名的 Index 提供（ix_ama_session/ix_ama_submission），
    # 与迁移脚本 0007_anchor_missing_ack 创建的索引名一致——这里不再重复用
    # index=True（会生成第二个自动命名的重复索引）。
    tender_list_session_id = Column(
        Integer, ForeignKey("tender_list_sessions.id"), nullable=False,
    )
    anchor_seq = Column(String(20), nullable=False)
    submission_id = Column(Integer, ForeignKey("bid_submissions.id"), nullable=False)

    reason = Column(String(500), default="")   # 预留：确认理由，本轮前端不填
    acked_by = Column(String(100), default="")

    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "tender_list_session_id", "anchor_seq", "submission_id",
            name="uq_anchor_missing_ack",
        ),
        Index("ix_ama_session", "tender_list_session_id"),
        Index("ix_ama_submission", "submission_id"),
    )
