"""TenderDocument ORM model — confirmed tender (招标文件) after user review.

Created when the user confirms a TENDER ExtractionJob's parsed items.
References the source job and (optionally) an existing Project.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("extraction_jobs.id"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)

    project_name: Mapped[str | None] = mapped_column(String(255), default="")
    project_code: Mapped[str | None] = mapped_column(String(64), default="")
    tender_date: Mapped[str | None] = mapped_column(String(32), default="")
    deadline: Mapped[str | None] = mapped_column(String(32), default="")

    items: Mapped[Any] = mapped_column(JSON, default=list, nullable=True)  # 材料清单 list[dict]
    # Deterministic recommendation/evidence snapshot used when this tender was
    # saved.  It makes an invitation auditable without creating an ERP master.
    recommendation_snapshot: Mapped[Any] = mapped_column(JSON, default=dict, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), default="draft", index=True)  # draft/invited/closed

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    invitations = relationship(
        "BidInvitation", back_populates="tender", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TenderDocument {self.id} {self.project_name}>"
