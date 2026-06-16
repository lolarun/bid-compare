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
    tender_list_session_id = Column(Integer, nullable=True)   # FK → tender_list_sessions.id
    anchor_seq = Column(String(20), nullable=True)            # TenderAnchor.seq (stringified)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    items = relationship("BidAlignmentItem", back_populates="group", cascade="all, delete-orphan")


class BidAlignmentItem(Base):
    """Maps a single quote_id into an alignment group."""

    __tablename__ = "bid_alignment_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("bid_alignment_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    action = Column(String(20), default="align")  # align / pending / exclude
    spec_note = Column(String(500), default="")
    # Aggregated pricing for multi-row same-anchor same-canonical items
    agg_total = Column(Float, nullable=True)  # Σ total_price
    agg_qty = Column(Float, nullable=True)    # Σ quantity
    name_note = Column(String(500), default="")

    created_at = Column(DateTime, default=_now)

    group = relationship("BidAlignmentGroup", back_populates="items")

    __table_args__ = (
        Index("ix_align_item_group_quote", "group_id", "quote_id", unique=True),
    )
