"""Bid alignment persistence models.

Stores user-confirmed alignment groups that map multiple quote rows
(from different suppliers) to a single canonical comparison line.

Design: does NOT overwrite original materials/quotes — keeps a
separate mapping so the alignment is reversible and traceable.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidAlignmentGroup(Base):
    """One canonical comparison line, grouping quotes from multiple suppliers."""

    __tablename__ = "bid_alignment_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(50), default="")

    # AI-suggested canonical name/spec (user may have edited)
    suggested_name: Mapped[str | None] = mapped_column(String(200), default="")
    suggested_spec: Mapped[str | None] = mapped_column(String(500), default="")
    suggested_unit: Mapped[str | None] = mapped_column(String(20), default="")
    suggested_qty: Mapped[float | None] = mapped_column(Float, nullable=True)

    confidence: Mapped[float | None] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text, default="")
    status: Mapped[str | None] = mapped_column(String(20), default="confirmed")  # confirmed / rejected / pending

    # Anchor linkage: composite key for unique identification across projects/versions
    tender_list_session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tender_list_sessions.id"), nullable=True)   # §11.2
    anchor_seq: Mapped[str | None] = mapped_column(String(20), nullable=True)            # TenderAnchor.seq (stringified)

    # docs/design/42 §4.1 (P2): group identity becomes (project_id, category,
    # round_id, anchor_uid). round_id makes the match wipe-and-rebuild
    # round-scoped — matching round 2 no longer deletes round 1's groups.
    # Nullable: legacy groups (pre-P2) and callers that don't pass a round
    # (e.g. preview_service.py, sandboxed and rolled back) keep round_id=NULL.
    # anchor_uid is the cross-round join key for round_trend — provenance,
    # not row identity within a round; empty when the anchor predates P1.
    round_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("quote_rounds.id"), nullable=True, index=True)
    anchor_uid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bid_alignment_groups.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quote_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("quotes.id"), nullable=True, index=True)
    bid_quote_line_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bid_quote_lines.id"), nullable=True, index=True,
    )
    supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=True)
    submission_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bid_submissions.id"), nullable=True, index=True)  # §11.2

    action: Mapped[str | None] = mapped_column(String(20), default="align")  # align / pending / exclude
    spec_note: Mapped[str | None] = mapped_column(String(500), default="")
    agg_total: Mapped[float | None] = mapped_column(Float, nullable=True)   # Σ total_price
    agg_qty: Mapped[float | None] = mapped_column(Float, nullable=True)     # Σ quantity
    name_note: Mapped[str | None] = mapped_column(String(500), default="")

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)

    group = relationship("BidAlignmentGroup", back_populates="items")
