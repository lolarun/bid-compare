"""AlignmentFinalization — 锚点对齐的 finalize 状态记录。

记录某次锚点匹配 + 复核结束后的最终状态，关联到具体的 BidAlignmentGroup 快照。
比价矩阵保存必须存在 status=finalized 的 AlignmentFinalization。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class AlignmentFinalization(Base):
    """一次对齐复核的最终状态。"""

    __tablename__ = "alignment_finalizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)  # §11.2
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)

    alignment_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 某次 import_and_match 批次标识
    group_ids_json: Mapped[Any] = mapped_column(JSON, nullable=True)            # finalize 时锁定的 group ID 列表

    status: Mapped[str | None] = mapped_column(
        String(30),
        default="draft_matching",
    )
    # draft_matching | review_required | review_passed | finalized

    pending_at_finalize: Mapped[int | None] = mapped_column(Integer, default=0)        # finalize 时剩余未处理 pending 数
    readiness_json: Mapped[Any] = mapped_column(JSON, nullable=True)            # list[QuoteReadiness] snapshot

    finalized_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    forced: Mapped[bool | None] = mapped_column(Boolean, default=False)
    force_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_af_project_category", "project_id", "category"),
    )
