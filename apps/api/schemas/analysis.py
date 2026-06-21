"""Analysis & comparison Pydantic schemas."""
from pydantic import BaseModel, Field, model_validator


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


# ─── Anchor Review Matrix (pre-review UI) ─────────────────────────────────────

class ReviewCellCandidate(BaseModel):
    item_id: int
    quote_id: int | None = None
    bid_quote_line_id: int | None = None
    material_name: str
    spec: str
    unit_price: float | None
    confidence: float | None
    flags: list[str] | None = None


class ReviewCell(BaseModel):
    cell_status: str  # quoted|aggregated|pending|excluded|missing
    item_id: int | None = None
    quote_id: int | None = None
    bid_quote_line_id: int | None = None
    unit_price: float | None = None
    total_price: float | None = None
    confidence: float | None = None
    evidence: str | None = None
    flags: list[str] | None = None
    is_lowest: bool = False
    candidates: list[ReviewCellCandidate] = []
    model_config = {"extra": "ignore"}


class ReviewRow(BaseModel):
    anchor_seq: str
    anchor_name: str
    anchor_spec: str
    unit: str
    quantity: float | None
    row_status: str  # ok|partial|pending|missing
    quoted_count: int
    covered_count: int
    cells: dict[str, ReviewCell]  # keyed by str(supplier_id)
    model_config = {"extra": "ignore"}


class ReviewSupplier(BaseModel):
    supplier_id: int
    supplier_name: str
    checksum_status: str | None = None
    declared_total: float | None = None
    checksum_delta_pct: float | None = None


class AnchorReviewMatrixResult(BaseModel):
    anchors_total: int
    supplier_count: int
    pending_cells: int
    missing_cells: int
    quoted_ge_2_count: int
    quoted_full_count: int
    suppliers: list[ReviewSupplier]
    matrix_distribution: "MatrixDistribution | None" = None
    rows: list[ReviewRow]
    model_config = {"extra": "ignore"}


# ─── Bid Matrix ───────────────────────────────────────────────────────────────

class SupplierCell(BaseModel):
    supplier_id: int
    price: float | None
    total: float | None
    deviation_pct: float | None
    alert_level: str
    is_lowest: bool
    # v2.5 anchor-matrix extended fields (all optional for backward compat)
    cell_status: str | None = None          # quoted|aggregated|pending|excluded|missing
    item_id: int | None = None              # pending cell: BidAlignmentItem.id
    confidence: float | None = None         # pending cell: cosine similarity
    source_quote_id: int | None = None      # old path: Quote.id
    bid_quote_line_id: int | None = None    # new path: BidQuoteLine.id
    pending_note: str | None = None         # "另有 N 条待确认" when align+pending coexist
    flags: list[str] | None = None          # validator flags: ocr_corrected_verified, valve_type_conflict, etc.
    evidence: str | None = None             # LLM fill reasoning/evidence stored in name_note

    model_config = {"extra": "ignore"}


class HistoricalAvg(BaseModel):
    price: float
    period: str
    projects: int


class ReasonableLowInfo(BaseModel):
    price: float
    date: str
    project: str


class MatrixRow(BaseModel):
    material_id: int | None       # None for anchor rows with no matched quote
    material_name: str
    spec: str
    anchor_seq: str | None = None  # v2.5: TenderAnchor.seq
    historical_avg: HistoricalAvg | None
    reasonable_low: ReasonableLowInfo | None
    suppliers: list[SupplierCell]
    min_deviation: float | None
    recommended: str | None

    model_config = {"extra": "ignore"}


class MatrixTotal(BaseModel):
    supplier_id: int
    total: float
    avg_deviation: float | None = None   # null when quoted_count=0 or no baseline
    quoted_count: int
    anomaly_count: int
    declared_total: float | None = None
    checksum_delta_pct: float | None = None
    checksum_status: str | None = None  # "pass" / "fail" / "unknown"


class BidMatrixRequest(BaseModel):
    project_id: int | None = None
    supplier_ids: list[int]
    submission_ids: list[int] = []
    material_ids: list[int] | None = None
    category: str | None = None


class SupplierLabel(BaseModel):
    id: int
    letter: str
    name: str


class MatrixDistribution(BaseModel):
    supplier_count: int
    anchors_total: int
    quoted_distribution: dict[str, int]   # keys "0".."N"
    covered_distribution: dict[str, int]
    quoted_ge_2_count: int    # 可比价锚点（quoted ≥2家）
    quoted_full_count: int    # N家完整自动比价（全部 N 家 quoted/aggregated）
    covered_ge_2_count: int   # covered ≥2家（quoted+pending，复核后可比价潜力）
    covered_full_count: int   # N家完整覆盖（含 pending，人工复核后潜在完整能力）


class BidMatrixResult(BaseModel):
    project_id: int | None
    suppliers: list[SupplierLabel]
    rows: list[MatrixRow]
    totals: list[MatrixTotal]
    brand_tier_filter: str | None = None
    # v2.5 meta
    anchor_matrix: bool | None = None
    not_finalized_warning: str | None = None
    matrix_distribution: MatrixDistribution | None = None
    # Recommendation gate
    recommendation_blocked: bool = False
    recommendation_blocked_reasons: list[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


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


# ─── Bid Alignment (AI 对齐复核) ──────────────────────────────────────────────

class AlignmentRowInput(BaseModel):
    quote_id: int
    supplier_id: int
    supplier_name: str = ""
    material_name: str = ""
    spec: str = ""
    unit: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    total_price: float | None = None


class AlignmentSuggestRequest(BaseModel):
    project_id: int | None = None
    category: str = ""
    supplier_ids: list[int] = []
    rows: list[AlignmentRowInput] = []


class AlignmentGroupItem(BaseModel):
    quote_id: int
    supplier_id: int
    action: str = "align"
    spec_note: str = ""
    name_note: str = ""


class AlignmentGroup(BaseModel):
    suggested_name: str = ""
    suggested_spec: str = ""
    confidence: float = 0.0
    reason: str = ""
    items: list[AlignmentGroupItem] = []


class AlignmentFieldFix(BaseModel):
    quote_id: int
    field: str = "unit_price"
    current: float | None = None
    suggested: float | None = None
    confidence: float = 0.0
    reason: str = ""


class AlignmentSuggestResult(BaseModel):
    groups: list[AlignmentGroup] = []
    field_fixes: list[AlignmentFieldFix] = []
    tokens_used: int = 0
    duration_ms: int = 0
    error: str = ""


class AlignmentApplyGroupItem(BaseModel):
    # 两路径互斥：quote_id（旧路径）或 bid_quote_line_id（新路径），必须且只有一个非空
    quote_id: int | None = None
    bid_quote_line_id: int | None = None
    supplier_id: int
    action: str = "align"
    spec_note: str = ""
    name_note: str = ""

    @model_validator(mode='after')
    def check_exactly_one(self) -> 'AlignmentApplyGroupItem':
        has_quote = self.quote_id is not None
        has_bql = self.bid_quote_line_id is not None
        if has_quote == has_bql:
            raise ValueError("Exactly one of quote_id or bid_quote_line_id must be set")
        return self


class AlignmentApplyGroup(BaseModel):
    suggested_name: str
    suggested_spec: str = ""
    suggested_unit: str = ""
    suggested_qty: float | None = None
    confidence: float = 0.0
    reason: str = ""
    status: str = "confirmed"  # confirmed / rejected
    items: list[AlignmentApplyGroupItem] = []


class AlignmentApplyFieldFix(BaseModel):
    quote_id: int
    field: str = "unit_price"
    new_value: float | None = None


class AlignmentApplyRequest(BaseModel):
    project_id: int | None = None
    category: str = ""
    groups: list[AlignmentApplyGroup] = []
    field_fixes: list[AlignmentApplyFieldFix] = []


class AlignmentApplyResult(BaseModel):
    groups_saved: int = 0
    items_saved: int = 0
    fixes_applied: int = 0
    error: str = ""


class AlignmentGroupOut(BaseModel):
    id: int
    project_id: int | None
    category: str
    suggested_name: str
    suggested_spec: str
    suggested_unit: str
    suggested_qty: float | None
    confidence: float
    reason: str
    status: str
    items: list[AlignmentGroupItem] = []
    model_config = {"from_attributes": True}


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
