"""AlignmentFinalization — 锚点对齐的 finalize 状态记录。

记录某次锚点匹配 + 复核结束后的最终状态，关联到具体的 BidAlignmentGroup 快照。
比价矩阵保存必须存在 status=finalized 的 AlignmentFinalization。
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, Text, Index, ForeignKey

from apps.api.core.database import Base
from apps.api.models._base import _now


class AlignmentFinalization(Base):
    """一次对齐复核的最终状态。"""

    __tablename__ = "alignment_finalizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)  # §11.2
    category = Column(String(50), default="", nullable=False)

    alignment_run_id = Column(String(100), nullable=True)  # 某次 import_and_match 批次标识
    group_ids_json = Column(JSON, nullable=True)            # finalize 时锁定的 group ID 列表

    status = Column(
        String(30),
        default="draft_matching",
    )
    # draft_matching | review_required | review_passed | finalized

    pending_at_finalize = Column(Integer, default=0)        # finalize 时剩余未处理 pending 数
    readiness_json = Column(JSON, nullable=True)            # list[QuoteReadiness] snapshot

    finalized_by = Column(String(100), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    forced = Column(Boolean, default=False)
    force_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_af_project_category", "project_id", "category"),
    )
