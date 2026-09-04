"""BidInvitation ORM model — recommended supplier invitations for a tender.

Created by SupplierRecommendService when the user saves a recommendation set.
One row per (tender, supplier) pair.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidInvitation(Base):
    __tablename__ = "bid_invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tender_id: Mapped[int] = mapped_column(Integer, ForeignKey("tender_documents.id"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=False, index=True)

    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[Any] = mapped_column(JSON, default=dict, nullable=True)  # {history_count, avg_deviation_pct, ...}
    status: Mapped[str | None] = mapped_column(String(16), default="pending", index=True)
    # pending / sent / responded / declined

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    tender = relationship("TenderDocument", back_populates="invitations")
    supplier = relationship("Supplier")

    __table_args__ = (
        UniqueConstraint("tender_id", "supplier_id", name="uq_tender_supplier"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BidInvitation tender={self.tender_id} sup={self.supplier_id} rank={self.rank}>"
