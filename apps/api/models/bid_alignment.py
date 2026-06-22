"""Bid alignment persistence models.

Stores user-confirmed alignment groups that map multiple quote rows
(from different suppliers) to a single canonical comparison line.

Design: does NOT overwrite original materials/quotes — keeps a
separate mapping so the alignment is reversible and traceable.
"""

from sqlalchemy import Column, Integer, Float, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidAlignmentGroup(Base):
    """One canonical comparison line, grouping quotes from multiple suppliers."""

    __tablename__ = "bid_alignment_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    category = Column(String(50), default="")

    # AI-suggested canonical name/spec (user may have edited)
    suggested_name = Column(String(200), default="")
    suggested_spec = Column(String(500), default="")
    suggested_unit = Column(String(20), default="")
    suggested_qty = Column(Float, nullable=True)

    confidence = Column(Float, default=0.0)
    reason = Column(Text, default="")
    status = Column(String(20), default="confirmed")  # confirmed / rejected / pending

    # Anchor linkage: composite key for unique identification across projects/versions
    tender_list_session_id = Column(Integer, ForeignKey("tender_list_sessions.id"), nullable=True)   # §11.2
    anchor_seq = Column(String(20), nullable=True)            # TenderAnchor.seq (stringified)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    items = relationship("BidAlignmentItem", back_populates="group", cascade="all, delete-orphan")


class BidAlignmentItem(Base):
    """Maps a single quote or bid_quote_line into an alignment group.

    两种路径，必须且只能有一个非空（由 _ensure_sqlite_schema 的 CHECK 约束保证）：
      quote_id          — 旧路径，兼容历史矩阵（旧数据保留，新比价不再写入）
      bid_quote_line_id — 新路径，新版比价全程使用

    查询规则：优先读 bid_quote_line_id；bid_quote_line_id IS NULL 时 fallback 到 quote_id。
    部分唯一索引由 _ensure_sqlite_schema() 创建（SQLite partial index 无法在 __table_args__ 跨方言定义）。
    """

    __tablename__ = "bid_alignment_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(
        Integer, ForeignKey("bid_alignment_groups.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=True, index=True)
    bid_quote_line_id = Column(
        Integer, ForeignKey("bid_quote_lines.id"), nullable=True, index=True,
    )
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    submission_id = Column(Integer, ForeignKey("bid_submissions.id"), nullable=True, index=True)  # §11.2

    action = Column(String(20), default="align")  # align / pending / exclude
    spec_note = Column(String(500), default="")
    agg_total = Column(Float, nullable=True)   # Σ total_price
    agg_qty = Column(Float, nullable=True)     # Σ quantity
    name_note = Column(String(500), default="")

    created_at = Column(DateTime, default=_now)

    group = relationship("BidAlignmentGroup", back_populates="items")
