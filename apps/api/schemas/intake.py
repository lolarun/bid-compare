"""Pydantic schemas for /api/intake endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class JobResponse(BaseModel):
    """ExtractionJob → API response shape (subset of ORM)."""

    id: str
    type: str
    status: str
    filename: str = ""
    file_size: int = 0
    context: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str = ""
    confidence: float | None = None
    provider: str = ""
    tokens_used: int = 0
    duration_ms: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int
