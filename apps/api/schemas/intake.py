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


class ClassifyTier0Response(BaseModel):
    """design/28 §3 Tier 0——瞬时、零模型调用的文件分类结果。

    xlsx/xls → verdict 是 tender_list/bid_list/uncertain 三选一（uncertain
    是合法答案，不是"分类失败"）。pdf → verdict 恒为 "document"，附带
    text_layer 信号；招标/投标要等 Tier 1（识别跑完之后）才判得出，这个
    字段不含糊——前端看到 "document" 就该知道还没分类完。
    """

    filename: str
    kind: str            # "excel" | "pdf" | "unsupported"
    verdict: str          # xlsx: tender_list/bid_list/uncertain；pdf: document；其他: unsupported
    confidence: str = ""  # xlsx: definitive/strong/ambiguous；pdf 留空（Tier 0 对 pdf 不产出置信度）
    text_layer: str = ""  # pdf 专属：native/scanned
    price_columns: list[str] = Field(default_factory=list)
    fill_rate: float | None = None
    row_count: int = 0
    reason: str = ""


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
