import axios from 'axios'
import { notification } from 'ant-design-vue'

const TOKEN_KEY = 'mempas_token'

const api = axios.create({
  baseURL: '/api',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// Request interceptor: attach token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  if (config.data instanceof FormData) {
    delete config.headers['Content-Type']
  }
  return config
})

// Response interceptor: error handling
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err.response?.status
    const msg = err.response?.data?.detail || err.message
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      notification.error({ message: '登录过期', description: '请重新登录' })
      window.location.href = '/login'
    } else if (status === 403) {
      notification.error({ message: '无权限', description: msg })
    } else if (status && status >= 500) {
      notification.error({ message: '服务器错误', description: msg })
    }
    return Promise.reject(err)
  },
)

export default api

// ─── Types ──────────────────────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

export interface Material {
  id: number
  material_code: string
  standard_name: string
  profession: string
  category: string
  sub_category: string
  spec: string
  material_type: string
  unit: string
  brand: string
  exec_standard: string
  status: string
  extended_attrs: Record<string, unknown>
  ref_price_low: number | null
  ref_price_avg: number | null
  ref_price_median: number | null
  ref_price_high: number | null
  price_cv: number | null
  deviation_threshold: number | null
  created_at: string | null
  updated_at: string | null
}

export interface Supplier {
  id: number
  name: string
  short_name: string
  contact: string
  phone: string
  categories: string[]
  supplier_type: string
  win_count: number
  cooperation_score: number
  remark: string
  created_at: string | null
  updated_at: string | null
}

export interface Project {
  id: number
  name: string
  code: string
  location: string
  status: string
  remark: string
  created_at: string | null
  updated_at: string | null
}

export interface Quote {
  id: number
  material_id: number
  supplier_id: number | null
  project_id: number | null
  unit_price: number | null
  unit_price_excl_tax: number | null
  tax_rate: number | null
  quantity: number | null
  total_price: number | null
  brand: string
  remark: string
  quote_date: string
  bid_status: string
  deviation_pct: number | null
  alert_level: string
  created_at: string | null
  updated_at: string | null
}

export interface CategoryStat {
  category: string
  profession: string
  total_materials: number
  total_quotes: number
  avg_price: number | null
  price_cv: number | null
  supplier_count: number
  project_count: number
}

export interface DashboardSummary {
  total_materials: number
  total_suppliers: number
  total_projects: number
  total_quotes: number
  category_stats: CategoryStat[]
}

export interface PriceCompareResult {
  category: string
  sub_category: string
  baseline_avg: number | null
  baseline_median: number | null
  baseline_low: number | null
  baseline_high: number | null
  new_price: number | null
  deviation_pct: number | null
  alert_level: string
  sample_count: number
}

export interface SupplierScore {
  supplier_id: number
  supplier_name: string
  price_score: number
  history_score: number
  completeness_score: number
  commercial_score: number
  total_score: number
  weights: Record<string, number>
}

export interface StandardizeResult {
  original: string
  standardized: string
  changes: string[]
}

export interface ExtendedAttrField {
  key: string
  label: string
  source: string
  role: string
}

export interface ExtendedAttrSchema {
  category: string
  fields: ExtendedAttrField[]
}

export interface ImportResult {
  status: string
  batch_id: string
  imported: number
  skipped: number
  errors: Record<string, unknown>[]
  supplier_ids: number[]
}

export interface QuoteStats {
  total: number
  avg_price: number | null
  min_price: number | null
  max_price: number | null
  alert_counts: Record<string, number>
}

export interface SubCategoryStat {
  sub_category: string
  count: number
  mean: number
  median: number
  std: number
  cv: number
  min: number
  max: number
  p10: number
  p90: number
  suggested_threshold: number
}

export interface CategoryDetailStats {
  category: string
  profession: string
  total_records: number
  valid_prices: number
  sub_categories: SubCategoryStat[]
}

export interface SupplierCompareItem {
  supplier_id: number
  supplier_name: string
  avg_price: number | null
  quote_count: number
  completeness: number
  score: SupplierScore
}

export interface MultiCompareResult {
  category: string
  suppliers: SupplierCompareItem[]
}

// ─── BidMatrix ───────────────────────────────────────────────────────────────

export type CellStatus = 'quoted' | 'aggregated' | 'pending' | 'excluded' | 'missing'

export interface SupplierCell {
  // B3（评审 identity-key rename）：supplier_id 历史上一直是"列身份"（submission
  // 模式下实际是 BidSubmission.id），名不副实。submission_id 是新增同义正名键，
  // submission 模式下 = supplier_id 的值，legacy 模式为 null。supplier_id 兼容期
  // 内保持原值不变——现有按 supplier_id 做的行内 join（cell.supplier_id===label.id）
  // 依然正确，不需要在兼容期内改写。
  supplier_id: number
  submission_id: number | null
  price: number | null
  total: number | null
  deviation_pct: number | null
  alert_level: string
  is_lowest: boolean
  // v2.5 anchor-matrix fields (null/undefined on legacy rows)
  cell_status?: CellStatus | null
  item_id?: number | null          // pending cell: BidAlignmentItem.id for inline confirm
  confidence?: number | null       // pending cell: cosine similarity
  source_quote_id?: number | null
  bid_quote_line_id?: number | null  // new path: BidQuoteLine.id
  pending_note?: string | null     // "另有 N 条待确认" when align+pending coexist
  flags?: string[] | null          // validator flags: ocr_corrected_verified, valve_type_conflict, etc.
  evidence?: string | null         // LLM fill reasoning/evidence
  // 评标口径（招标数量×含税单价）+ 同规格偏差
  price_basis?: string | null
  incl_unit?: number | null        // 含税单价（评标用）
  unit?: string | null             // 供应商报价单位
  supplier_qty?: number | null     // 供应商报价数量（评标数量以 tender_qty 为准）
  item_canonical?: Record<string, unknown> | null
  tender_qty?: number | null       // 招标数量（评标数量，非供应商报价数量）
  eval_amount?: number | null      // 评标金额 = 招标数量×含税单价
  eval_status?: string | null      // ok|quantity_source_conflict|basis_unconfirmed|alignment_pending|missing
  evaluable?: boolean | null
  baseline?: { median: number; count: number; basis: string; spec_key: string } | null
  tax_basis_assumed?: boolean | null  // 单一价格列按招标含税要求假定纳入（非确认）
}

export interface MatrixRow {
  material_id: number | null
  material_name: string
  spec: string
  anchor_seq?: string | null       // v2.5: links row back to TenderAnchor.seq
  historical_avg: { price: number; period: string; projects: number } | null
  reasonable_low: { price: number; date: string; project: string } | null
  suppliers: SupplierCell[]
  min_deviation: number | null
  recommended: string | null
}

export interface BidMatrixMeta {
  anchor_matrix?: boolean          // v2.5: true when using anchor-full-axis mode
  not_finalized_warning?: string
}

export interface MatrixTotal {
  // B3：见 SupplierCell 顶部注释，同一条 col_id/submission_id 正名规则。
  supplier_id: number
  submission_id: number | null
  total: number
  avg_deviation: number | null  // null when quoted_count=0（无报价时不计偏差）
  quoted_count?: number
  anomaly_count?: number
  declared_total?: number | null
  checksum_delta_pct?: number | null
  checksum_status?: string | null   // "pass" / "fail" / "unknown"
  // 评标口径（招标数量×含税单价）
  evaluated_total?: number | null
  confirmed_lines?: number | null
  qty_conflict_lines?: number | null
  undecided_lines?: number | null
  undecided_amount?: number | null
  tax_assumed_lines?: number | null  // 单一价格列按招标含税要求纳入（假定非确认）
  basis_confirmed?: boolean | null
  eligible_for_ranking?: boolean | null
}

export interface SupplierLabel {
  id: number                         // column key: submission_id when available, else supplier_id
  letter: string
  name: string
  supplier_id?: number | null        // 真正的供应商 FK（submission 模式下可空）
  submission_id?: number | null      // B3：与 id 对称，submission 模式下等于 id
}

export interface BidInsight {
  overall: string
  recommendations: string[]
  risks: string[]
  tokens_used?: number
  duration_ms?: number
  error?: string
}

export interface MatrixDistribution {
  supplier_count: number
  anchors_total: number
  quoted_distribution: Record<string, number>   // keys "0".."N"
  covered_distribution: Record<string, number>
  quoted_ge_2_count: number    // 可比价锚点（quoted ≥2家）
  quoted_full_count: number    // N家完整自动比价
  covered_ge_2_count: number   // covered ≥2家（复核后可比价潜力）
  covered_full_count: number   // N家完整覆盖（含 pending）
}

export interface SupplierEvaluation {
  supplier_id: number
  submission_id: number | null   // B3：同上，与 supplier_id 同义正名
  name: string | null
  letter: string | null
  evaluated_total: number
  confirmed_lines: number
  total_anchors: number
  qty_conflict_lines: number
  undecided_lines: number
  undecided_amount: number
  missing_lines: number
  anomaly_count: number
  tax_assumed_lines: number
  basis_confirmed: boolean
  checksum_status: string
  full_coverage: boolean
  eligible_for_ranking: boolean
}

export interface CommonComparable {
  supplier_ids: number[]
  submission_ids: number[] | null  // B3：同义正名，submission 模式下等于 supplier_ids
  line_count: number
  subtotals: Record<string, number>
}

export interface NonPriceFactor {
  factor: string
  evidence_status: string
}

export interface BidMatrixResult {
  project_id: number | null
  suppliers: SupplierLabel[]
  rows: MatrixRow[]
  totals: MatrixTotal[]
  brand_tier_filter: string | null
  // v2.5 meta
  anchor_matrix?: boolean
  not_finalized_warning?: string
  matrix_distribution?: MatrixDistribution
  // Recommendation gate（兼容旧前端：仅 blocked 置 true）
  recommendation_blocked?: boolean
  recommendation_blocked_reasons?: string[]
  // 招标文件驱动的三态评标
  recommendation_level?: 'firm' | 'conditional' | 'blocked' | null
  recommendation_reasons?: string[]
  risks?: string[]
  evaluation_policy?: Record<string, unknown> | null
  award_mode?: string | null
  committee_required?: boolean | null
  price_ranking?: SupplierEvaluation[]
  price_preferred_candidate?: SupplierEvaluation | null
  supplier_evaluation?: SupplierEvaluation[]
  common_comparable?: CommonComparable | null
  non_price_factors?: NonPriceFactor[]
  comprehensive_recommendation_status?: string | null
}

// ─── Intake / Invite (Phase 2-3) ─────────────────────────────────────────────

export type IngestionType = 'tender' | 'quote' | 'tender_bidlist'
export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

// 招标文件 PDF 投标清单抽取结果（ExtractionJob.result for type=tender_bidlist）
export interface TenderBrandReq { brand_en: string; brand_cn: string }
export interface TenderSupplierBrand { supplier_name: string; brand: string; supplier_id?: number | null }

// 评审 R2（第4块）：input_mode 生产上恒为 'vl_direct'（vl_quote.py 里唯一的
// 赋值点）——'table_grid'/'html_fallback' 是已删除 legacy 逐页链路的遗留值，
// 只可能出现在旧快照回放里。此前联合类型没声明 'vl_direct'，二元判断把它
// 全部误判成 'html_fallback' 分支，UI 上把当前唯一的正式识别路径标成橙色
// 「OCR增强解析」——不是极端情况，是每一页的常态。
export interface PageDiagnostic {
  page: number
  input_mode: 'vl_direct' | 'table_grid' | 'html_fallback' | string
  fallback_reason: string
  expected_rows: number
  extracted_rows: number
  thinking_retry: boolean
}

export interface PdfQualityMetrics {
  seq_missing: number[]
  seq_duplicate: number[]
  material_columns_filled_rate: number
  brand_filled_rate: number
  source_ref_coverage: number
  qty_parse_success_rate: number
  row_count_by_page: Record<string, number>
  vl_direct_pages: number[]
  table_grid_pages: number[]
  html_fallback_pages: Array<{ page: number; fallback_reason: string }>
}

export interface TenderBidlistResult {
  items: Array<Record<string, unknown>>
  brand_requirement: TenderBrandReq[]
  supplier_brands: TenderSupplierBrand[]
  material_class: string
  detected_category?: string
  detected_pages: { bidlist: number[]; brand: number | null }
  row_count: number
  source_type: string
  quality_metrics?: PdfQualityMetrics | null
  page_diagnostics?: PageDiagnostic[] | null
}

// Excel vs PDF 对账结果
export interface SourceReconcileMismatch {
  seq: string
  field: string
  xlsx_value: string
  pdf_value: string
}
export interface SourceReconcileResult {
  xlsx_count: number
  pdf_count: number
  seq_missing_in_pdf: string[]
  only_in_excel_reference?: string[]   // pdf_primary 时：Excel 独有行（不进主清单）
  seq_missing_in_xlsx: string[]
  field_mismatches: SourceReconcileMismatch[]
  recommended_source: 'both_consistent' | 'excel' | 'pdf'
}

export interface ExtractionJob {
  id: string
  type: IngestionType
  status: JobStatus
  filename: string
  file_size: number
  context: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string
  confidence: number | null
  progress_stage?: string
  progress_pct?: number
  provider: string
  tokens_used: number
  duration_ms: number
  created_at: string | null
  updated_at: string | null
}

// 前端评审 R2（第1块）：quality_status/quality_blocking_reasons/row_ledger/
// orientation_unresolved 此前在 document_ingestion.py 就被丢弃，从未到达
// job.result。后端已修复（_merge_quality_metadata，写入 job.result._quality），
// 这里补上对应的 TS 类型，供 R2 后续几块的质量分层横幅/行标记使用。
export interface RowLedgerPageDrop {
  page: number
  role?: string
  reason: string
  expected: number
  extracted?: number
  lost?: number
  rotation_applied?: boolean
}

export interface RowLedger {
  target_pages: number
  expected_rows: number
  recognized_rows: number
  dropped_rows: number
  empty_pages: RowLedgerPageDrop[]
  short_pages: RowLedgerPageDrop[]
}

export interface QualityMeta {
  doc_type?: string
  quality_status?: 'PASS' | 'REVIEW' | 'BLOCKED' | string
  quality_blocking_reasons?: string[]
  page_count?: number
  target_pages?: number[]
  row_ledger?: RowLedger | null
  rotations?: Record<string, number> | null
  orientation_unresolved?: number[] | null
  recognizer?: string
}

export interface TenderExtractionItem {
  name: string
  category: string
  spec: string
  unit: string
  quantity: number | null
  remark: string
  extended_attrs?: Record<string, unknown>
}

export interface QuoteExtractionItem {
  material: string
  spec: string
  brand: string
  unit: string
  qty: number | null
  unit_price: number | null
  unit_price_excl_tax: number | null
  total_price: number | null
  tax_rate: number | null
  // 价格口径桥接字段（§4/§9）：必须随 item 完整往返到 batch-confirm，否则含税/不含税
  // 口径在网页端被裁掉，凯硕/泰科龙会按错误口径入库、绵存部分行变成无价格。
  unit_price_incl_tax?: number | null
  total_price_incl_tax?: number | null
  total_price_excl_tax?: number | null
  tax_amount?: number | null
  price_basis?: string
  effective_unit_price?: number | null
  effective_total_price?: number | null
  // 算术校验审计：原 qty 不改，suggested_qty 仅参考
  validation_flags?: string[]
  raw_qty?: number | null
  suggested_qty?: number | null
  // 全局文档行序（1..N）：顺序直连对齐的行身份，必须往返到 batch-confirm，不能丢。
  document_row_index?: number | null
  material_type?: string
  remark: string
  // hidden fields — never displayed in UI but must round-trip to batch-confirm
  // intact so that canonical / validation_warning reach anchor-match unchanged.
  // source_ref is preserved until batch-confirm; it is persisted into
  // Quote.extraction_meta_json so LLM supplier-fill judging can trace evidence.
  canonical?: Record<string, unknown>
  validation_warning?: string
  source_ref?: Record<string, unknown>
  // Layer 1 OCR correction: raw text stays in material; corrected name here
  normalized_material?: string
  ocr_correction_reason?: string
  // optional normalisation fields that batch-confirm may write back
  category?: string
  standard_name?: string
  standard_spec?: string
}

export interface RecommendReason {
  history_count: number
  history_score: number
  avg_deviation_pct: number | null
  price_score: number
  overall_score: number
  summary: string
  brands: string[]
  tags: string[]
}

export interface BrandRecommendation {
  brand_name: string
  tier: string        // 合资 | 国产
  category: string
  sample_count: number
  price_median: number | null
  price_p10: number | null
  price_p90: number | null
  tags: string[]
}

export interface SupplierRecommendation {
  supplier_id: number
  supplier_name: string
  score: number
  rank: number
  reason: RecommendReason
}

export interface RecommendResponse {
  categories: string[]
  recommendations: BrandRecommendation[]
  total_candidates: number
  supplier_recommendations: SupplierRecommendation[]
  total_supplier_candidates: number
  data_gaps: string[]
}

export interface BatchConfirmResult {
  status: string
  submission_id: number
  line_count: number
  skipped_count: number
  errors: Array<{ row: number; reason: string }>
  unknown_brands: string[]
  supplier_id: number | null
  project_id: number | null
  batch_id: string
  idempotent?: boolean
}

export interface SavedInvitation {
  id: number
  supplier_id: number
  supplier_name: string
  rank: number | null
  score: number | null
  status: string
}

export interface SaveInvitationsResponse {
  tender_id: number
  invitations: SavedInvitation[]
}

// ─── BrandTier ───────────────────────────────────────────────────────────────

export interface BrandTier {
  id: number
  brand_name: string
  tier: '国产' | '合资' | '三档'
  category: string | null
}

// ─── User / Log ──────────────────────────────────────────────────────────────

export interface User {
  id: number
  username: string
  nickname: string
  role: '管理员' | '比价员' | '查看者'
  email: string
  phone: string
  status: '启用' | '停用'
  last_login: string
}

export interface LogEntry {
  id: number
  time: string
  user: string
  module: string
  action: string
  target: string
  result: '成功' | '失败'
  remark: string
}

// ─── Invite ──────────────────────────────────────────────────────────────────

export interface InviteRecommendation {
  supplier_id: number
  supplier_name: string
  reason: string
  score: number
  price_advantage: string
  tags: string[]
}

export interface InviteResult {
  recommendations: InviteRecommendation[]
}

// ─── OCR ─────────────────────────────────────────────────────────────────────

export interface OcrItem {
  material: string
  spec: string
  brand: string
  unit: string
  qty: number
  price: number
}

export interface OcrResult {
  items: OcrItem[]
  batch_id: string | null
}

// ─── AI Enhance (OCR post-processing) ────────────────────────────────────────

export interface EnhancedItem {
  material: string
  spec: string
  brand: string
  unit: string
  qty: number | null
  unit_price: number | null
  unit_price_excl_tax: number | null
  total_price: number | null
  tax_rate: number | null
  remark: string
  // AI-added fields
  category: string
  standard_name: string
  original_name: string
  standard_spec: string
  original_spec: string
  name_note: string
  alignment_note: string
  matched_material_id: number | null
}

export interface EnhanceSummary {
  total: number
  categorized: number
  renamed: number
  aligned: number
  errors: number
}

export interface EnhanceResponse {
  items: EnhancedItem[]
  summary: EnhanceSummary
  tokens_used: number
  duration_ms: number
  error: string
}

// ─── Bid Alignment ─────────────────────────────────────────────────────────

export interface AlignmentRowInput {
  quote_id: number
  supplier_id: number
  supplier_name: string
  material_name: string
  spec: string
  unit: string
  quantity: number | null
  unit_price: number | null
  total_price: number | null
}

export interface AlignmentGroupItem {
  quote_id?: number | null
  bid_quote_line_id?: number | null
  supplier_id: number
  action: string
  spec_note?: string
  name_note?: string
}

export interface AlignmentGroup {
  suggested_name: string
  suggested_spec: string
  confidence: number
  reason: string
  items: AlignmentGroupItem[]
}

export interface AlignmentFieldFix {
  quote_id: number
  field: string
  current: number | null
  suggested: number | null
  confidence: number
  reason: string
}

export interface AlignmentSuggestResult {
  groups: AlignmentGroup[]
  field_fixes: AlignmentFieldFix[]
  tokens_used: number
  duration_ms: number
  error: string
}

export interface AlignmentApplyGroup {
  suggested_name: string
  suggested_spec: string
  suggested_unit?: string
  suggested_qty?: number | null
  confidence: number
  reason: string
  status: 'confirmed' | 'rejected'
  items: AlignmentGroupItem[]
}

export interface AlignmentApplyFieldFix {
  quote_id: number
  field: string
  new_value: number | null
}

export interface AlignmentApplyResult {
  groups_saved: number
  items_saved: number
  fixes_applied: number
  error: string
}

export interface AlignmentGroupOut {
  id: number
  project_id: number | null
  category: string
  suggested_name: string
  suggested_spec: string
  suggested_unit: string
  suggested_qty: number | null
  confidence: number
  reason: string
  status: string
  items: AlignmentGroupItem[]
}

// ─── Anchor / Tender-list matching ──────────────────────────────────────────

export interface TenderPreviewItem {
  seq: string
  name: string
  spec: string
  model: string
  pressure: string
  materials: Record<string, string>
  unit: string
  qty: number | null
  brand?: string              // PDF 清单要求品牌（可空）
  profession: string
  remark?: string
  category: string            // 品类识别结果（"" = 待人工确认）
  category_confidence: number
  category_reason: string
  canonical: Record<string, string>
  source_ref?: Record<string, unknown>
}

export interface TenderPreviewResult {
  items: TenderPreviewItem[]
  detected_category: string
  category_breakdown: Record<string, number>
  has_multiple_categories: boolean
  unknown_count: number
  total: number
}

export interface TenderListConfirmSession {
  category: string
  id: number
  version: number
  anchors_total: number
}

export interface TenderListCurrentSession {
  id: number
  category: string
  anchors_total: number
}

/** /analysis/compare-state — 刷新可恢复：项目供应商报价步骤的全部进度 */
export interface CompareStateSubmission {
  submission_id: number
  job_id: string | null
  filename: string
  supplier_raw_name: string
  supplier_id: number | null
  status: string
  line_count: number
  batch_id: string | null
  job_status: string
  progress_stage: string
  progress_pct: number
}
export interface CompareStateInflightJob {
  job_id: string
  filename: string
  status: string
  progress_stage: string
  progress_pct: number
  has_result: boolean
}
export interface CompareStateResult {
  submissions: CompareStateSubmission[]
  inflight_jobs: CompareStateInflightJob[]
}

export interface AnchorMatchSummary {
  anchors_total: number
  anchors_covered: number
  comparable_2plus: number
  three_way: number
  matched_quotes: number
  total_quotes: number
  low_conf: number
  residue: number
  category?: string
}

export interface SupplierFillSummary {
  supplier_id: number
  supplier_name: string
  quoted: number
  aggregated: number
  pending: number
  excluded: number
  residue: number
  residue_high_cos: number
  dropped: number
  tokens_used: number
  duration_ms: number
  error: string | null
}

export interface LlmFillResult {
  anchors_total: number
  comparable_2plus: number
  three_way: number
  anchors_covered: number
  comparable_2plus_embedding_baseline: number
  per_supplier_fill: SupplierFillSummary[]
  finalization_invalidated: boolean
  dropped_audit: Array<{ supplier_id: number; quote_id?: number | null; anchor_seq?: number | null; reason: string }>
  matrix_distribution?: MatrixDistribution
}

// ─── Anchor Review Matrix ────────────────────────────────────────────────────

export interface ReviewCellCandidate {
  item_id: number
  quote_id: number
  material_name: string
  spec: string
  unit_price: number | null
  confidence: number | null
  flags: string[] | null
}

export interface ReviewCell {
  cell_status: 'quoted' | 'aggregated' | 'pending' | 'excluded' | 'missing'
  item_id: number | null
  quote_id: number | null
  unit_price: number | null
  total_price: number | null
  confidence: number | null
  evidence: string | null
  flags: string[] | null
  is_lowest: boolean
  candidates: ReviewCellCandidate[]
  missing_reason?: string | null
  // design/23：复核者已确认"这格确实无报价，符合预期"——纯 UI 抑制标记，
  // 只在 cell_status='missing' 时有意义，不改变 cell_status 本身。
  missing_acked?: boolean
}

export interface ReviewRow {
  anchor_seq: string
  anchor_name: string
  anchor_spec: string
  anchor_pressure?: string
  anchor_materials?: string
  anchor_brand?: string
  unit: string
  quantity: number | null
  row_status: 'ok' | 'partial' | 'pending' | 'missing'
  quoted_count: number
  covered_count: number
  cells: Record<string, ReviewCell>   // keyed by str(supplier_id)
}

export interface ReviewSupplier {
  submission_id: number               // §7 authoritative column identity
  supplier_id: number | null          // nullable soft-ref (may be null for unlinked submissions)
  supplier_name: string               // = supplier_raw_name
  supplier_raw_name: string
  brand?: string
  checksum_status: string | null
  declared_total: number | null
  checksum_delta_pct: number | null
}

export interface AnchorReviewMatrixResult {
  anchors_total: number
  supplier_count: number
  pending_cells: number
  missing_cells: number
  quoted_ge_2_count: number
  quoted_full_count: number
  suppliers: ReviewSupplier[]
  brand_requirement?: { brand_en: string; brand_cn: string }[]
  matrix_distribution?: MatrixDistribution
  rows: ReviewRow[]
}

export interface AnchorGroupItem {
  item_id: number
  action: 'align' | 'pending' | 'exclude'
  quote_id: number
  supplier_id: number
  supplier_name: string
  material_name: string
  spec: string
  unit_price: number | null
  cosine: number | null
  spec_note: string
}

export interface AnchorReviewGroup {
  group_id: number
  anchor_name: string
  anchor_spec: string
  confidence: number
  items: AnchorGroupItem[]
  pending_count?: number   // only on low_conf_groups entries
  align_count?: number
}

export interface AnchorResidueQuote {
  quote_id: number
  supplier_id: number
  supplier_name: string
  material_name: string
  spec: string
  unit_price: number | null
}

export interface AnchorReviewResult {
  low_conf_groups: AnchorReviewGroup[]
  confirmed_groups: AnchorReviewGroup[]
  residue_quotes: AnchorResidueQuote[]
  pending_items_total: number
}

// ─── Dashboard visualisation ────────────────────────────────────────────────

export interface TreeChild {
  name: string
  value: number
}

export interface TreeNode {
  name: string
  value: number
  children: TreeChild[]
}

export interface DashboardHeatmapData {
  nodes: TreeNode[]
}

export interface BubbleChild {
  name: string
  amount: number
  tier: string | null
}

export interface DashboardBubbleItem {
  name: string
  profession: string
  total_amount: number
  children: BubbleChild[]
}

export interface DashboardBubbleData {
  items: DashboardBubbleItem[]
}
