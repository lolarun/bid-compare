"""BidMatrixVersion — 比价矩阵的版本快照。

每次保存比价结果时创建一条记录，关联采购清单版本 + 对齐 finalization。
用于审批、追溯和导出。
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, Index, ForeignKey

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidMatrixVersion(Base):
    """比价矩阵的一个已保存版本。"""

    __tablename__ = "bid_matrix_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, nullable=True, index=True)
    category = Column(String(50), default="", nullable=False)
    version = Column(Integer, default=1)                    # 同 project+category 下自增

    # 追溯引用
    tender_list_session_id = Column(
        Integer,
        ForeignKey("tender_list_sessions.id"),
        nullable=True,
        index=True,
    )
    alignment_finalization_id = Column(
        Integer,
        ForeignKey("alignment_finalizations.id"),
        nullable=True,
        index=True,
    )

    # 矩阵快照
    matrix_json = Column(JSON, nullable=True)           # 完整矩阵（rows, totals, suppliers）
    readiness_json = Column(JSON, nullable=True)        # list[QuoteReadiness] at save time
    anchors_count = Column(Integer, default=0)
    compared_rows = Column(Integer, default=0)
    excluded_rows_json = Column(JSON, nullable=True)    # {pending, residue, validation_failed}
    supplier_ids_json = Column(JSON, nullable=True)
    recommended_supplier = Column(String(200), nullable=True)

    # 审批状态
    status = Column(String(20), default="preview")      # preview | reviewed | approved
    review_note = Column(Text, nullable=True)
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_bmv_project_category", "project_id", "category"),
    )
