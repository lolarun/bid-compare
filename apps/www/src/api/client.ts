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
  brand_score: number
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

export interface SupplierCell {
  supplier_id: number
  price: number | null
  total: number | null
  deviation_pct: number | null
  alert_level: string
  is_lowest: boolean
}

export interface MatrixRow {
  material_id: number
  material_name: string
  spec: string
  historical_avg: { price: number; period: string; projects: number } | null
  reasonable_low: { price: number; date: string; project: string } | null
  suppliers: SupplierCell[]
  min_deviation: number | null
  recommended: string | null
}

export interface MatrixTotal {
  supplier_id: number
  total: number
  avg_deviation: number
}

export interface BidMatrixResult {
  project_id: number | null
  suppliers: { id: number; letter: string; name: string }[]
  rows: MatrixRow[]
  totals: MatrixTotal[]
}

// ─── Intake / Invite (Phase 2-3) ─────────────────────────────────────────────

export type IngestionType = 'tender' | 'quote'
export type JobStatus = 'pending' | 'running' | 'done' | 'failed'

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
  provider: string
  tokens_used: number
  duration_ms: number
  created_at: string | null
  updated_at: string | null
}

export interface TenderExtractionItem {
  name: string
  category: string
  spec: string
  unit: string
  quantity: number | null
  remark: string
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
  remark: string
}

export interface RecommendReason {
  history_count: number
  history_score: number
  avg_deviation_pct: number | null
  price_score: number
  overall_score: number
  brand_score: number
  summary: string
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
  recommendations: SupplierRecommendation[]
}

export interface BatchConfirmResult {
  status: string
  created: number
  skipped: number
  errors: Array<{ row: number; reason: string }>
  unknown_brands: string[]
  quote_ids: number[]
  supplier_id: number | null
  project_id: number | null
  batch_id: string
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
  tier: '一档' | '二档' | '三档'
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
