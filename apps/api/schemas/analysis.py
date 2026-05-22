"""Analysis & comparison Pydantic schemas."""
from pydantic import BaseModel


class PriceCompareRequest(BaseModel):
    category: str
    sub_category: str | None = None
    spec: str | None = None
    new_price: float | None = None


class PriceCompareResult(BaseModel):
    category: str
    sub_category: str
    reasonable_low: float | None
    reasonable_low_project: str | None
    reasonable_low_date: str | None
    historical_avg: float | None
    historical_median: float | None
    historical_min: float | None
    baseline_high: float | None
    new_price: float | None
    deviation_pct: float | None
    alert_level: str
    sample_count: int


class SupplierScoreRequest(BaseModel):
    supplier_id: int
    project_id: int | None = None
    category: str | None = None
    weights: dict[str, float] | None = None


class SupplierScoreResult(BaseModel):
    supplier_id: int
    supplier_name: str
    price_score: float
    history_score: float
    completeness_score: float
    brand_score: float
    commercial_score: float
    total_score: float
    weights: dict


class CategoryStats(BaseModel):
    category: str
    profession: str
    total_materials: int
    total_quotes: int
    avg_price: float | None
    price_cv: float | None
    supplier_count: int
    project_count: int


class DashboardSummary(BaseModel):
    total_materials: int
    total_suppliers: int
    total_projects: int
    total_quotes: int
    category_stats: list[CategoryStats]


class MultiCompareRequest(BaseModel):
    supplier_ids: list[int]
    category: str
    project_id: int | None = None
    weights: dict[str, float] | None = None


class SupplierCompareItem(BaseModel):
    supplier_id: int
    supplier_name: str
    avg_price: float | None
    quote_count: int
    completeness: float
    score: SupplierScoreResult


class MultiCompareResult(BaseModel):
    category: str
    suppliers: list[SupplierCompareItem]


class SubCategoryStat(BaseModel):
    sub_category: str
    count: int
    mean: float
    median: float
    std: float
    cv: float
    min: float
    max: float
    p10: float
    p90: float
    suggested_threshold: float


class CategoryDetailStats(BaseModel):
    category: str
    profession: str
    total_records: int
    valid_prices: int
    sub_categories: list[SubCategoryStat]


# ─── Bid Matrix ───────────────────────────────────────────────────────────────

class SupplierCell(BaseModel):
    supplier_id: int
    price: float | None
    total: float | None
    deviation_pct: float | None
    alert_level: str
    is_lowest: bool


class HistoricalAvg(BaseModel):
    price: float
    period: str
    projects: int


class ReasonableLowInfo(BaseModel):
    price: float
    date: str
    project: str


class MatrixRow(BaseModel):
    material_id: int
    material_name: str
    spec: str
    historical_avg: HistoricalAvg | None
    reasonable_low: ReasonableLowInfo | None
    suppliers: list[SupplierCell]
    min_deviation: float | None
    recommended: str | None


class MatrixTotal(BaseModel):
    supplier_id: int
    total: float
    avg_deviation: float
    quoted_count: int
    anomaly_count: int


class BidMatrixRequest(BaseModel):
    project_id: int | None = None
    supplier_ids: list[int]
    material_ids: list[int] | None = None
    category: str | None = None


class SupplierLabel(BaseModel):
    id: int
    letter: str
    name: str


class BidMatrixResult(BaseModel):
    project_id: int | None
    suppliers: list[SupplierLabel]
    rows: list[MatrixRow]
    totals: list[MatrixTotal]


# ─── Bid Insight (AI Analysis) ────────────────────────────────────────────────

class BidInsightRequest(BaseModel):
    """Accepts the full bid-matrix result for AI analysis."""
    project_id: int | None = None
    suppliers: list[SupplierLabel]
    rows: list[MatrixRow]
    totals: list[MatrixTotal]


class BidInsightResult(BaseModel):
    overall: str = ""
    recommendations: list[str] = []
    risks: list[str] = []
    tokens_used: int = 0
    duration_ms: int = 0
    error: str = ""


# ─── BrandTier ────────────────────────────────────────────────────────────────

class BrandTierCreate(BaseModel):
    brand_name: str
    tier: str
    category: str | None = None


class BrandTierUpdate(BaseModel):
    tier: str | None = None
    category: str | None = None


class BrandTierOut(BaseModel):
    id: int
    brand_name: str
    tier: str
    category: str | None
    model_config = {"from_attributes": True}


# ─── Config ───────────────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    value: dict
    description: str | None = None


class ConfigOut(BaseModel):
    id: int
    key: str
    value: dict
    description: str
    updated_at: object | None = None
    model_config = {"from_attributes": True}


# ─── Dashboard visualisation ─────────────────────────────────────────────────

class TreeChild(BaseModel):
    name: str
    value: float


class TreeNode(BaseModel):
    name: str
    value: float
    children: list[TreeChild] = []


class DashboardHeatmapData(BaseModel):
    nodes: list[TreeNode]


class BubbleChild(BaseModel):
    name: str
    amount: float
    tier: str | None = None


class BubbleItem(BaseModel):
    name: str
    profession: str
    total_amount: float
    children: list[BubbleChild] = []


class DashboardBubbleData(BaseModel):
    items: list[BubbleItem]
