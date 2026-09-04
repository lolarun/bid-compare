"""Quote Pydantic schemas."""

from datetime import datetime

from pydantic import BaseModel


class QuoteBase(BaseModel):
    material_id: int
    supplier_id: int | None = None
    project_id: int | None = None
    unit_price: float | None = None
    unit_price_excl_tax: float | None = None
    tax_rate: float | None = None
    quantity: float | None = None
    total_price: float | None = None
    brand: str = ""
    brand_tier: str = ""
    remark: str = ""
    quote_date: str = ""
    bid_status: str = ""


class QuoteCreate(QuoteBase):
    pass


class QuoteUpdate(BaseModel):
    unit_price: float | None = None
    unit_price_excl_tax: float | None = None
    tax_rate: float | None = None
    quantity: float | None = None
    total_price: float | None = None
    brand: str | None = None
    remark: str | None = None
    quote_date: str | None = None
    supplier_id: int | None = None
    project_id: int | None = None


class QuoteOut(QuoteBase):
    id: int
    batch_id: str = ""
    bid_status: str = ""
    deviation_pct: float | None = None
    alert_level: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ─── GET /api/quotes（list_quotes）────────────────────────────────────────
# 评审 E4 residue：routes/quotes.py 只在关系被 eager-load 到时才把这些字段塞进
# dict（`if i.material: d["material_name"] = ...`），故全部可选。前端
# history/IndexView.vue 的本地 QuoteRow 类型只声明了 material_name/spec/
# supplier_name/unit 四个（表格 dataIndex 实际读取的）；category/profession/
# project_name 后端算了但前端未读——按 Tier 2 batch-confirm 先例（evidence-chain，
# CLAUDE.md §4）原样声明，不因前端未读而在 schema 里砍掉。
class QuoteListItemOut(QuoteOut):
    material_name: str | None = None
    spec: str | None = None
    unit: str | None = None
    category: str | None = None
    profession: str | None = None
    supplier_name: str | None = None
    project_name: str | None = None


class QuoteListResult(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[QuoteListItemOut]


# ─── GET /api/quotes/batches（list_batches）───────────────────────────────
class QuoteBatchItemOut(BaseModel):
    batch_id: str
    count: int
    created_at: str | None = None
    supplier_id: int | None = None
    supplier_name: str = ""
    project_id: int | None = None
    project_name: str = ""


class QuoteBatchListResult(BaseModel):
    items: list[QuoteBatchItemOut]
    total: int


# ─── GET /api/quotes/stats（quote_stats）──────────────────────────────────
# 前端 quoteApi.stats 目前零真实调用方（仅单测断言 URL/params，非 UI 流程）——
# 与 Tier 2 llm-fill 同款：后端已算的字段照实声明，不因未读而砍。
class QuoteStatsResult(BaseModel):
    total: int
    avg_price: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    alert_counts: dict[str, int] = {}


# ─── POST /api/quotes/archive-prices（archive_prices）─────────────────────
# 三态 status 与逐字段均由 test_bql_e2e.py 的断言核实（"archived"/
# "partially_archived"/"no_eligible"；archived_count/skipped_count 精确核对）。
# 前端零调用方（无 quoteApi.archivePrices 绑定）——同样按 evidence-chain 全量声明。
class ArchivePricesSkippedLineOut(BaseModel):
    line_id: int
    reason: str


class ArchivePricesResult(BaseModel):
    status: str
    submission_id: int
    eligible_count: int
    archived_count: int
    skipped_count: int
    already_archived_count: int
    skipped_lines: list[ArchivePricesSkippedLineOut] = []
