"""BidInvitation ORM model — recommended supplier invitations for a tender.

Created by SupplierRecommendService when the user saves a recommendation set.
One row per (tender, supplier) pair.
"""

from sqlalchemy import Column, Integer, Float, String, DateTime, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidInvitation(Base):
    __tablename__ = "bid_invitations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tender_documents.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False, index=True)

    score = Column(Float, nullable=True)
    rank = Column(Integer, nullable=True)
    reason = Column(JSON, default=dict)  # {history_count, avg_deviation_pct, ...}
    status = Column(String(16), default="pending", index=True)
    # pending / sent / responded / declined

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    tender = relationship("TenderDocument", back_populates="invitations")
    supplier = relationship("Supplier")

    __table_args__ = (
        UniqueConstraint("tender_id", "supplier_id", name="uq_tender_supplier"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BidInvitation tender={self.tender_id} sup={self.supplier_id} rank={self.rank}>"
