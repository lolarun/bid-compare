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
    # design/23：复核者已确认"这格确实无报价，符合预期"——纯 UI 抑制标记，
    # 不改变 cell_status 本身，不参与评标总价。只在 cell_status=missing 时
    # 有意义；查 AnchorMissingAck 表填充。
    missing_acked: bool = False
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
    cells: dict[str, ReviewCell]  # keyed by str(col_id)：submission 模式下为 submission_id
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


# ─── Anchor missing acknowledgment (docs/design/23) ────────────────────────────

class AnchorMissingAckRequest(BaseModel):
    project_id: int
    category: str
    anchor_seq: str
    submission_id: int
    acked: bool
    reason: str = ""  # 预留：本轮前端不传


class AnchorMissingAckResult(BaseModel):
    ok: bool
    anchor_seq: str
    submission_id: int
    acked: bool


# ─── Bid Matrix ───────────────────────────────────────────────────────────────

class SupplierCell(BaseModel):
    # B3 兼容期收尾（design/22 §B3，三个触发条件核实后完成）：原来的
    # supplier_id 键名历史上一直是"列身份"（submission 模式下实际是
    # BidSubmission.id，legacy 模式下才真是 Supplier.id）——名不副实，且没有
    # 任何消费方在这个粒度需要真正的供应商 FK（真正的供应商 FK 在
    # SupplierLabel.supplier_id 上，那个字段不受影响）。改为通用列身份键
    # `id`（= submission_id when available, else supplier_id），与
    # SupplierLabel 的 id 语义对称；submission_id 保留，legacy 模式下为 None。
    id: int
    submission_id: int | None = None
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
    unit: str | None = None                 # 供应商报价单位（评标资格判定用）
    supplier_qty: float | None = None       # 供应商报价数量（评标数量以招标 tender_qty 为准）
    item_canonical: dict | None = None      # 对齐行的规格 canonical（阀型/DN/PN）
    tender_qty: float | None = None         # 招标数量（评标数量，非供应商报价数量）
    eval_amount: float | None = None        # 评标金额 = 招标数量×含税单价
    eval_status: str | None = None          # ok|quantity_source_conflict|basis_unconfirmed|alignment_pending|missing
    evaluable: bool | None = None
    baseline: dict | None = None            # 同规格基准 {median,count,basis,spec_key}
    tax_basis_assumed: bool | None = None   # 单一价格列按招标含税要求假定纳入（非确认）

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
    # B3 兼容期收尾：见 SupplierCell 顶部注释，同一条 id/submission_id 规则。
    id: int
    submission_id: int | None = None
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
    id: int                            # column key: submission_id when available, else supplier_id
    letter: str
    name: str
    supplier_id: int | None = None     # 真正的供应商 FK（submission 模式下可空——陌生供应商未绑定）
    submission_id: int | None = None   # B3：与 id 对称，submission 模式下等于 id，legacy 模式为 None


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


class TenderSheetInfoOut(BaseModel):
    name: str
    looks_like_list: bool
    row_count: int


class TenderPreviewResultOut(BaseModel):
    items: list[TenderPreviewItemOut]
    detected_category: str
    category_breakdown: dict[str, int]
    has_multiple_categories: bool
    unknown_count: int
    total: int
    # design/24 B1：多 Sheet 支持——候选 Sheet 列表 + 本次实际用的那个，
    # 前端据此渲染 Sheet 切换器；单 Sheet 文件 sheets 长度为 1，行为不变。
    sheets: list[TenderSheetInfoOut] = []
    selected_sheet: str | None = None


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


# ─── tender-list/llm-fill ───────────────────────────────────────────────────
# 评审 E4 Tier 2：用户预判该端点响应字段多、易触发止损规则；核实后发现前端
# analysisApi.tenderListLlmFill 从未被任何 .vue/.ts 调用(grep 全 www/src 零命中，
# 仅 CSS 里两个孤儿类名)，backend 测试(test_llm_fill_endpoint.py /
# test_llm_fill_persistence.py) 也都是直接调用路由函数或 _persist_llm_fill，
# 不经过 TestClient HTTP 层，response_model 序列化对它们零影响。
# 结论：这是"零消费方"端点，不存在止损规则针对的隐藏耦合，可以按 Python 实际
# 返回值直接建完整 schema，不必因为字段多而拆去 Tier 3。
# dropped_audit/missing_audit/false_positive_audit 是审计明细，各 supplier 的
# "reason" 分支形状不同(不同错误类型携带不同附加键)，故意保留为 list[dict] 而
# 非强 schema，避免为一个无人消费的调试字段编造不存在的稳定契约。

class SupplierFillSummaryOut(BaseModel):
    supplier_id: int
    supplier_name: str
    quoted: int
    aggregated: int
    pending: int
    excluded: int
    residue: int
    residue_high_cos: int
    dropped: int
    tokens_used: int
    duration_ms: int
    error: str | None = None


class LlmFillReadinessOut(BaseModel):
    can_finalize: bool
    false_positive_align_count: int
    missing_without_evidence_count: int
    supplier_error_count: int
    warnings: list[str] = []


class LlmFillResult(BaseModel):
    anchors_total: int
    comparable_2plus: int
    comparable_2plus_quoted: int
    three_way: int
    anchors_covered: int
    comparable_2plus_embedding_baseline: int
    per_supplier_fill: list[SupplierFillSummaryOut] = []
    finalization_invalidated: bool
    dropped_audit: list[dict] = []
    missing_audit: list[dict] = []
    missing_audit_total: int = 0
    missing_audit_truncated: bool = False
    false_positive_audit: list[dict] = []
    false_positive_align_count: int
    readiness: LlmFillReadinessOut
    matrix_distribution: MatrixDistribution | None = None


# ─── tender-list/match ──────────────────────────────────────────────────────
# 评审 E4 Tier 2：核实后同样是"零隐藏耦合"——前端 tenderMatchSummary 是严格
# ref<AnchorMatchSummary>，编译期已保证只读 TS 声明的 8 个基础字段 + category；
# 后端 HTTP 测试(test_bql_e2e.py/test_compare_integration.py/
# test_vl_direct_api_e2e.py)在 match 响应本身上也只断言 status_code，字段级
# 断言全部落在别的端点(如 GET tender-list/current 的 used_submission_ids，
# 已在 Tier 1 覆盖)。readiness_list/per_supplier_stats 是路由自己拼的审计
# 附加信息，前后端都不读，同 llm-fill 一样保留为松散 dict，不为无人消费的字段
# 编造精确契约。

class TenderMatchResult(BaseModel):
    anchors_total: int
    anchors_covered: int
    comparable_2plus: int
    three_way: int
    matched_quotes: int
    total_quotes: int
    low_conf: int
    residue: int
    category: str | None = None
    readiness_list: list[dict] = []
    per_supplier_stats: dict = {}
    model_config = {"extra": "ignore"}


# ─── /api/quotes/batch-confirm ──────────────────────────────────────────────
# 评审 E4 Tier 2：此前 response_model=dict——语法上"有"，实际零校验/零文档
# 价值，评审把它归为"缺契约"是对的。核实前端 quoteApi.batchConfirm 的 3 个
# 调用点（compare/IndexView.vue ×2、import/IndexView.vue ×1），均严格走既有
# 的 BatchConfirmResult TS 类型，无 Record<string,any> 或 as any 旁路读取。
# 但 confirm_batch() 实际有 3 条返回路径（幂等命中 / 空 items / 正常写入），
# 并集比前端 TS 类型多 5 个字段：checksum(幂等路径)、missing_total_rows/
# not_quoted_rows/not_quoted_detail/integrity(正常写入路径)——这些是价格闭环
# 门和结构完整性门(doc/19 §L4)的审计结果，属于 CLAUDE.md §4"任何自动修正必
# 须保留原值/依据/标记"要求的证据链，即使当前前端不读也不能让 response_model
# 把它们从响应体里悄悄吃掉，故补全字段而非按 TS 类型精简。checksum/integrity
# 内部形状来自 _build_checksum/_gate_integrity，服务层返回裸 dict，未强 schema
# 化，这里保持一致(dict)不重复发明。

class BatchConfirmErrorOut(BaseModel):
    row: int
    reason: str


class BatchConfirmResult(BaseModel):
    status: str
    submission_id: int
    line_count: int
    skipped_count: int
    errors: list[BatchConfirmErrorOut] = []
    unknown_brands: list[str] = []
    supplier_id: int | None = None
    project_id: int | None = None
    batch_id: str
    idempotent: bool | None = None
    checksum: dict | None = None
    missing_total_rows: int | None = None
    not_quoted_rows: int | None = None
    not_quoted_detail: list[dict] | None = None
    integrity: dict | None = None
    # design/24 B0：非 None = 识别到多份合法副本（copy_no），本次只选了其中
    # 一份入库。同一条 §4 证据链要求——不能让 response_model 把这条悄悄吃掉。
    copy_dedup: dict | None = None
    # design/24 B3：dry_run=true 时的响应形状——从不写库，issues 收集本次会
    # 命中的全部结构性疑点（不是只有第一个）。真实写入路径这四个字段恒为
    # None/false，前端用 dry_run 字段本身判断走哪条渲染分支。
    dry_run: bool | None = None
    would_succeed: bool | None = None
    issues: list[dict] = []
    already_stored: bool | None = None
    model_config = {"extra": "ignore"}


# ─── bid-matrix version snapshots (B3 Tier 3) ──────────────────────────────
# 评审 E4 Tier 3：此前不单排、并入 B3——SupplierCell.supplier_id/MatrixTotal.
# supplier_id 是 B3 要改的错名字段，先接 response_model 会把错误契约固化成
# 正式契约。B3 已完成identity-key 正名（见 SupplierCell/MatrixTotal/
# SupplierLabel 顶部注释），现在补 response_model 是同一轮的后半步，不是
# 提前动作。matrix_json 是持久化的 BidMatrixResult 快照，快照本身走
# BidMatrixResult schema 保证；这里的版本包装端点只对壳字段（id/version/
# status/…）建约束，matrix_json/readiness_json/excluded_rows_json/
# supplier_ids_json 保持裸 dict/list——它们是存量快照的整体读写，不逐字段
# 消费，不为此另建一份重复的强 schema。

class BidMatrixSaveResult(BaseModel):
    ok: bool
    id: int
    version: int


class BidMatrixVersionListItem(BaseModel):
    id: int
    version: int
    status: str
    anchors_count: int
    compared_rows: int
    recommended_supplier: str | None = None
    approved_by: str | None = None
    approved_at: object | None = None
    created_at: object | None = None


class BidMatrixVersionDetail(BaseModel):
    id: int
    version: int
    status: str
    project_id: int | None = None
    category: str
    tender_list_session_id: int | None = None
    alignment_finalization_id: int | None = None
    matrix_json: dict = {}
    readiness_json: list = []
    anchors_count: int
    compared_rows: int
    excluded_rows_json: list = []
    supplier_ids_json: list = []
    recommended_supplier: str | None = None
    review_note: str | None = None
    approved_by: str | None = None
    approved_at: object | None = None
    created_at: object | None = None


class BidMatrixVersionApproveResult(BaseModel):
    ok: bool
    id: int
    status: str
