"""BidMatrixVersion — 比价矩阵的版本快照。

每次保存比价结果时创建一条记录，关联采购清单版本 + 对齐 finalization。
用于审批、追溯和导出。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidMatrixVersion(Base):
    """比价矩阵的一个已保存版本。"""

    __tablename__ = "bid_matrix_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)  # §11.2
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    version: Mapped[int | None] = mapped_column(Integer, default=1)                    # 同 project+category 下自增

    # 追溯引用
    tender_list_session_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("tender_list_sessions.id"),
        nullable=True,
        index=True,
    )
    alignment_finalization_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("alignment_finalizations.id"),
        nullable=True,
        index=True,
    )

    # 矩阵快照
    matrix_json: Mapped[Any] = mapped_column(JSON, nullable=True)           # 完整矩阵（rows, totals, suppliers）
    readiness_json: Mapped[Any] = mapped_column(JSON, nullable=True)        # list[QuoteReadiness] at save time
    anchors_count: Mapped[int | None] = mapped_column(Integer, default=0)
    compared_rows: Mapped[int | None] = mapped_column(Integer, default=0)
    excluded_rows_json: Mapped[Any] = mapped_column(JSON, nullable=True)    # {pending, residue, validation_failed}
    supplier_ids_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    recommended_supplier: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 审批状态
    status: Mapped[str | None] = mapped_column(String(20), default="preview")      # preview | reviewed | approved
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_bmv_project_category", "project_id", "category"),
    )
