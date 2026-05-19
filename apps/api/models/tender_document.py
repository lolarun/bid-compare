"""TenderDocument ORM model — confirmed tender (招标文件) after user review.

Created when the user confirms a TENDER ExtractionJob's parsed items.
References the source job and (optionally) an existing Project.
"""

from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("extraction_jobs.id"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)

    project_name = Column(String(255), default="")
    project_code = Column(String(64), default="")
    tender_date = Column(String(32), default="")
    deadline = Column(String(32), default="")

    items = Column(JSON, default=list)  # 材料清单 list[dict]
    status = Column(String(16), default="draft", index=True)  # draft/invited/closed

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    invitations = relationship(
        "BidInvitation", back_populates="tender", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TenderDocument {self.id} {self.project_name}>"
