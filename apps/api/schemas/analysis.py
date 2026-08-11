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
    comparison_profile: str = "standard"
    review_hint: str = ""


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
    # 评审 E4：cell_status=missing 时 bid_matrix.py 会补一句人话解释（"该供应商
    # 未报价此品项" / "清单此项无比价组..."），前端 AnchorReviewMatrix.vue:342
    # 读它做兜底文案；schema 之前没声明，核实时发现补上。
    missing_reason: str | None = None
    model_config = {"extra": "ignore"}


class ReviewRow(BaseModel):
    anchor_seq: str
    anchor_name: str
    anchor_spec: str
    # 评审 E4：build_anchor_review_matrix 的行字典实际还有这三个字段，前端
    # AnchorReviewMatrix.vue 也在读（client.ts 的 TS 类型已经声明了它们），
    # 但这份 Pydantic schema 之前漏了——核实时发现，先补全再接 response_model，
    # 否则会把锚点的压力等级/材质/品牌要求悄悄丢给前端。
    anchor_pressure: str = ""
    anchor_materials: str = ""
    anchor_brand: str = ""
    unit: str
    quantity: float | None
    row_status: str  # ok|partial|pending|missing
    quoted_count: int
    covered_count: int
    cells: dict[str, ReviewCell]  # keyed by str(supplier_id)
    model_config = {"extra": "ignore"}


class ReviewSupplier(BaseModel):
    # 评审 E4：submission_id 是新 BID 路径的权威列身份（§7），前端拿它做列 key
    # 和跨行的 cell 查找（AnchorReviewMatrix.vue 多处 sup.submission_id）；
    # supplier_raw_name/brand 前端也在读（展示名优先取它、品牌标签）。三个都是
    # bid_matrix.py 返回值里的真实字段，schema 之前漏了——先补全再接
    # response_model，否则前端复核矩阵的列匹配会直接失效。
    submission_id: int | None = None  # legacy supplier-path 下为 None
    supplier_id: int | None = None    # nullable soft-ref（submission 未关联正式供应商时）
    supplier_name: str
    supplier_raw_name: str = ""
    brand: str = ""
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
    # 招标品牌要求（{brand_en, brand_cn} 列表）。评审 E4：此前本 schema 已写好但
    # 零引用，核实时发现漏了这个字段——build_anchor_review_matrix 的返回值里有
    # brand_requirement，前端 AnchorReviewMatrix.vue:230-232 也在读，若直接接
    # response_model 会把它悄悄丢掉。补上后再接。
    brand_requirement: list[dict] = []
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
    # 评标口径（招标数量×含税单价）+ 同规格偏差
    price_basis: str | None = None
    incl_unit: float | None = None          # 含税单价（评标用）
    tender_qty: float | None = None         # 招标数量（评标数量，非供应商报价数量）
    eval_amount: float | None = None        # 评标金额 = 招标数量×含税单价
    eval_status: str | None = None          # ok|quantity_source_conflict|basis_unconfirmed|alignment_pending|missing
    evaluable: bool | None = None
    baseline: dict | None = None            # 同规格基准 {median,count,basis,spec_key}

    model_config = {"extra": "ignore"}


class HistoricalAvg(BaseModel):
    price: float
    period: str = ""
    projects: int = 0
    # 同规格基准（新口径：median 即展示值=偏差计算值）
    spec_key: str | None = None
    count: int | None = None
    basis: str | None = None


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
    reasonable_low: ReasonableLowInfo | None = None
    spec_baseline: HistoricalAvg | None = None
    suppliers: list[SupplierCell]
    min_deviation: float | None
    recommended: str | None
    # anchor 展示
    unit: str | None = None
    quantity: float | None = None
    materials: str | None = None
    brand: str | None = None

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
    # 评标口径（招标数量×含税单价）
    evaluated_total: float | None = None
    confirmed_lines: int | None = None
    qty_conflict_lines: int | None = None
    undecided_lines: int | None = None
    undecided_amount: float | None = None
    tax_assumed_lines: int | None = None   # 单一价格列按招标含税要求纳入（假定非确认）
    basis_confirmed: bool | None = None
    eligible_for_ranking: bool | None = None


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
    # Recommendation gate（兼容旧前端：仅 blocked 置 true）
    recommendation_blocked: bool = False
    recommendation_blocked_reasons: list[str] = Field(default_factory=list)
    # 招标文件驱动的三态评标
    recommendation_level: str | None = None   # firm | conditional | blocked
    recommendation_reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evaluation_policy: dict | None = None
    award_mode: str | None = None
    committee_required: bool | None = None
    price_ranking: list[dict] = Field(default_factory=list)
    price_preferred_candidate: dict | None = None
    supplier_evaluation: list[dict] = Field(default_factory=list)
    common_comparable: dict | None = None
    non_price_factors: list[dict] = Field(default_factory=list)
    comprehensive_recommendation_status: str | None = None

    model_config = {"extra": "ignore"}


# ─── Bid Insight (AI Analysis) ────────────────────────────────────────────────

class BidInsightRequest(BaseModel):
    """Accepts the full bid-matrix result for AI analysis."""
    project_id: int | None = None
    suppliers: list[SupplierLabel]
    rows: list[MatrixRow]
    totals: list[MatrixTotal]
    # 评标上下文（AI 只能据此解释，不得改选）
    recommendation_level: str | None = None
    evaluation_policy: dict | None = None
    award_mode: str | None = None
    committee_required: bool | None = None
    price_ranking: list[dict] = Field(default_factory=list)
    price_preferred_candidate: dict | None = None
    supplier_evaluation: list[dict] = Field(default_factory=list)
    common_comparable: dict | None = None
    non_price_factors: list[dict] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


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


# ─── Anchor review (legacy group/item view, GET /anchor-review) ───────────────
# 评审 E4 Tier 1：与 AnchorReviewMatrixResult 是两套不同的复核视图，命名对齐
# 前端 client.ts 的 AnchorGroupItem/AnchorReviewGroup/AnchorResidueQuote/
# AnchorReviewResult。bid_quote_line_id 未出现在前端 TS 类型里，但
# test_bql_e2e.py 对它有断言(BQL 新路径身份字段)，必须保留、不可让
# response_model 静默丢弃。

class AnchorGroupItemOut(BaseModel):
    item_id: int
    action: str
    quote_id: int | None = None
    bid_quote_line_id: int | None = None
    supplier_id: int | None = None
    supplier_name: str
    material_name: str
    spec: str
    unit_price: float | None
    cosine: float | None = None
    spec_note: str = ""


class AnchorReviewGroupOut(BaseModel):
    group_id: int
    anchor_name: str
    anchor_spec: str
    confidence: float
    items: list[AnchorGroupItemOut] = []
    pending_count: int | None = None
    align_count: int | None = None


class AnchorResidueQuoteOut(BaseModel):
    quote_id: int | None = None
    bid_quote_line_id: int | None = None
    supplier_id: int | None = None
    supplier_name: str
    material_name: str
    spec: str
    unit_price: float | None


class AnchorReviewResult(BaseModel):
    low_conf_groups: list[AnchorReviewGroupOut] = []
    confirmed_groups: list[AnchorReviewGroupOut] = []
    residue_quotes: list[AnchorResidueQuoteOut] = []
    pending_items_total: int = 0


# ─── Anchor review confirm / item-confirm / bulk-confirm / finalize ───────────

class AnchorReviewConfirmResult(BaseModel):
    ok: bool
    group_id: int
    status: str  # "confirmed" | "deleted"


class AnchorReviewItemConfirmResult(BaseModel):
    ok: bool
    item_id: int
    action: str  # "align" | "exclude"


class AnchorReviewBulkConfirmResult(BaseModel):
    ok: bool
    confirmed: int


class AnchorReviewFinalizeResult(BaseModel):
    ok: bool
    id: int
    status: str
    group_ids_count: int
    pending_at_finalize: int


# ─── Bid-alignment group delete / baseline refresh (trivial status dicts) ─────

class BidAlignmentGroupDeleteResult(BaseModel):
    status: str
    deleted_group_id: int


class RefreshBaselinesResult(BaseModel):
    status: str
    message: str


# ─── Tender-list preview / reconcile / confirm / sessions ─────────────────────

class TenderPreviewItemOut(BaseModel):
    seq: str
    name: str
    spec: str
    model: str
    pressure: str
    materials: dict[str, str] = {}
    unit: str
    qty: float | None
    profession: str
    category: str
    category_confidence: float
    category_reason: str
    canonical: dict[str, str | None] = {}


class TenderPreviewResultOut(BaseModel):
    items: list[TenderPreviewItemOut]
    detected_category: str
    category_breakdown: dict[str, int]
    has_multiple_categories: bool
    unknown_count: int
    total: int


class SourceReconcileMismatchOut(BaseModel):
    seq: str
    field: str
    xlsx_value: str
    pdf_value: str


class SourceReconcileResultOut(BaseModel):
    xlsx_count: int
    pdf_count: int
    seq_missing_in_pdf: list[str]
    only_in_excel_reference: list[str] | None = None
    seq_missing_in_xlsx: list[str]
    field_mismatches: list[SourceReconcileMismatchOut]
    recommended_source: str


class TenderListConfirmSessionOut(BaseModel):
    category: str
    id: int
    version: int
    anchors_total: int


class TenderListConfirmResult(BaseModel):
    ok: bool
    id: int
    version: int
    primary_category: str
    sessions: list[TenderListConfirmSessionOut]
    multi_category: bool


class TenderListCurrentSessionOut(BaseModel):
    id: int
    category: str
    anchors_total: int


class TenderListCurrentSessionsResult(BaseModel):
    sessions: list[TenderListCurrentSessionOut]
    primary_category: str


class TenderListCurrentResult(BaseModel):
    id: int
    version: int
    category: str
    file_name: str
    anchors_total: int
    status: str
    confirmed_by: str | None = None
    confirmed_at: object | None = None
    created_at: object | None = None
    used_submission_ids: list[int] = []


class TenderListDeactivateResult(BaseModel):
    ok: bool
    deactivated: int


class TenderListVersionOut(BaseModel):
    id: int
    version: int
    is_current: bool
    status: str
    anchors_total: int
    file_name: str
    created_at: object | None = None


# ─── Compare-state (refresh-recoverable progress) ──────────────────────────────

class CompareStateSubmissionOut(BaseModel):
    submission_id: int
    job_id: str | None = None
    filename: str
    supplier_raw_name: str
    supplier_id: int | None = None
    status: str
    line_count: int
    batch_id: str | None = None
    job_status: str
    progress_stage: str
    progress_pct: float


class CompareStateInflightJobOut(BaseModel):
    job_id: str
    filename: str
    status: str
    progress_stage: str
    progress_pct: float
    has_result: bool


class CompareStateResult(BaseModel):
    submissions: list[CompareStateSubmissionOut]
    inflight_jobs: list[CompareStateInflightJobOut]
