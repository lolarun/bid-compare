"""ExtractionJob ORM model — tracks async document-extraction tasks.

Lifecycle: pending → running → done / failed
Used by DocumentIngestionService to:
- enqueue (file_hash idempotency)
- run in background (via FastAPI BackgroundTasks)
- query state from frontend (polling endpoint)
"""

from sqlalchemy import Column, String, Integer, Float, Text, DateTime, JSON, Index

from apps.api.core.database import Base
from apps.api.models._base import _now


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id = Column(String(36), primary_key=True)  # UUID4 hex
    type = Column(String(16), nullable=False, index=True)  # 'tender' | 'quote'
    status = Column(String(16), nullable=False, default="pending", index=True)

    # File
    filename = Column(String(255), default="")
    file_hash = Column(String(64), index=True)  # SHA256 of content
    file_size = Column(Integer, default=0)
    file_path = Column(String(512), default="")
    mime_type = Column(String(64), default="")

    # Business context (project_id, supplier_id, category)
    context = Column(JSON, default=dict)

    # Extraction result
    result = Column(JSON, nullable=True)
    error = Column(Text, default="")
    confidence = Column(Float, nullable=True)

    # Provider telemetry
    provider = Column(String(64), default="")
    tokens_used = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_job_type_status", "type", "status"),
        Index("ix_job_hash_type", "file_hash", "type"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtractionJob {self.id} {self.type} {self.status}>"
