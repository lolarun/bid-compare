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


class SummarizeFactsRequest(BaseModel):
    """design/29 §4——工作台卡片概述。facts 是**已经抽取、已经确认过**的
    结构化字段（project_name/row_count 这些），不是原始文件——这个接口
    不碰识别，只把已有事实组织成一两句话。"""

    kind: str          # "tender" | "bid"
    facts: dict[str, Any] = Field(default_factory=dict)


class SummarizeFactsResponse(BaseModel):
    summary: str


class ClassifyTier0Response(BaseModel):
    """design/28 §3 Tier 0 + design/29 §3 Tier 1.5 的合并判定结果。

    xlsx/xls → verdict 是 tender_list/bid_list/uncertain 三选一（uncertain
    是合法答案，不是"分类失败"）。

    pdf → verdict 是 tender/bid/uncertain 三选一（design/29 前是恒为
    "document"，现在 Tier 1.5 接进来了）：原生文字层 PDF 走零模型调用的
    封面关键词判据，能给出真实判定；扫描件**不调用**视觉模型（design/29
    §3.1 实测 0/7，证明这条路径不可靠，调用它只会给一个比不猜还差的偏向
    性错误答案）——扫描件恒为 uncertain，前端看到 uncertain 就该弹"这是
    招标文件还是投标文件？"两选一，不是等它自己判出来。
    """

    filename: str
    kind: str            # "excel" | "pdf" | "unsupported"
    verdict: str          # xlsx: tender_list/bid_list/uncertain；pdf: tender/bid/uncertain；其他: unsupported
    confidence: str = ""  # xlsx: definitive/strong/ambiguous；pdf 留空（Tier 1.5 对 pdf 不产出置信度分档，只有能判/不能判两种）
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
