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
    progress_stage: str = ""
    progress_pct: int = 0
    provider: str = ""
    tokens_used: int = 0
    duration_ms: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int


# ── Enhance (AI post-processing) ─────────────────────────────────────────────

class EnhanceRequest(BaseModel):
    """Request body for POST /api/intake/enhance."""
    job_id: str | None = None          # load items from a completed job
    project_id: int | None = None      # for pre-alignment against existing quotes
    items: list[dict[str, Any]] | None = None  # override items (skip job lookup)


class EnhancedItem(BaseModel):
    """A single OCR item with AI enhancements."""
    # Original OCR fields
    material: str = ""
    spec: str = ""
    brand: str = ""
    unit: str = ""
    qty: float | None = None
    unit_price: float | None = None
    unit_price_excl_tax: float | None = None
    total_price: float | None = None
    tax_rate: float | None = None
    remark: str = ""
    # AI-enhanced fields
    category: str = ""
    standard_name: str = ""
    original_name: str = ""
    standard_spec: str = ""
    original_spec: str = ""
    name_note: str = ""             # explanation of name change
    alignment_note: str = ""        # pre-alignment match info
    matched_material_id: int | None = None


class EnhanceSummary(BaseModel):
    """Statistics about AI enhancements applied."""
    total: int = 0
    categorized: int = 0
    renamed: int = 0
    aligned: int = 0
    errors: int = 0


class EnhanceResponse(BaseModel):
    """Response from POST /api/intake/enhance."""
    items: list[EnhancedItem] = Field(default_factory=list)
    summary: EnhanceSummary = Field(default_factory=EnhanceSummary)
    tokens_used: int = 0
    duration_ms: int = 0
    error: str = ""
