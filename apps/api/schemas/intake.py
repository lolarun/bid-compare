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
    # design/24 B2：阶段内进度。stage_total=None 且 stage_current 有值 = "只有
    # 单调递增计数、没有总数"（如逐页识别的长生成阶段——已转录行数）；两个都有
    # 值 = 真正的"第 N/共 M"（如渲染页面）；两个都是 None = 这个阶段没有细粒度
    # 进度可报，前端退回只显示 progress_stage/progress_pct。
    stage_current: int | None = None
    stage_total: int | None = None
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
    # Canonical key (valve type / DN / PN / material / connection)
    canonical: dict = Field(default_factory=dict)
    # Row-level arithmetic validation result (empty = OK)
    validation_warning: str = ""
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
