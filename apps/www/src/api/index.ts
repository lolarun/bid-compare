import api from './client'
import type { AxiosRequestConfig } from 'axios'
import type {
  PaginatedResponse, Material, Supplier, Project, Quote,
  DashboardSummary, PriceCompareResult, SupplierScore,
  StandardizeResult, ExtendedAttrSchema, ImportResult,
  QuoteStats, CategoryDetailStats, MultiCompareResult,
  BidMatrixResult, BidInsight, BrandTier, User, LogEntry,
  InviteResult, OcrResult,
  ExtractionJob, RecommendResponse, BatchConfirmResult,
  SaveInvitationsResponse,
  DashboardHeatmapData, DashboardBubbleData,
  AlignmentRowInput, AlignmentSuggestResult,
  AlignmentApplyGroup, AlignmentApplyFieldFix, AlignmentApplyResult,
  AlignmentGroupOut,
  EnhanceResponse,
  AnchorMatchSummary, AnchorReviewResult, AnchorReviewMatrixResult, TenderPreviewResult, LlmFillResult,
  TenderListConfirmSession, SourceReconcileResult,
} from './client'

// ─── Materials ──────────────────────────────────────────────────────────────

export const materialApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<Material>>('/materials', { params }),
  get: (id: number) =>
    api.get<Material>(`/materials/${id}`),
  create: (data: Partial<Material>) =>
    api.post<Material>('/materials', data),
  update: (id: number, data: Partial<Material>) =>
    api.put<Material>(`/materials/${id}`, data),
  disable: (id: number) =>
    api.post<Material>(`/materials/${id}/disable`),
  delete: (id: number) =>
    api.delete(`/materials/${id}`),
  categories: () =>
    api.get<{ profession: string; category: string; count: number }[]>('/materials/categories'),
  standardize: (data: { text: string; category?: string }) =>
    api.post<StandardizeResult>('/materials/standardize', data),
  extendedSchema: (category: string) =>
    api.get<ExtendedAttrSchema>(`/materials/extended-schema/${category}`),
}

// ─── Suppliers ──────────────────────────────────────────────────────────────

export const supplierApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<Supplier>>('/suppliers', { params }),
  get: (id: number) =>
    api.get<Supplier>(`/suppliers/${id}`),
  create: (data: Partial<Supplier>) =>
    api.post<Supplier>('/suppliers', data),
  update: (id: number, data: Partial<Supplier>) =>
    api.put<Supplier>(`/suppliers/${id}`, data),
  delete: (id: number) =>
    api.delete(`/suppliers/${id}`),
}

// ─── Projects ───────────────────────────────────────────────────────────────

export const projectApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<Project>>('/projects', { params }),
  get: (id: number) =>
    api.get<Project>(`/projects/${id}`),
  create: (data: Partial<Project>) =>
    api.post<Project>('/projects', data),
  update: (id: number, data: Partial<Project>) =>
    api.put<Project>(`/projects/${id}`, data),
  delete: (id: number) =>
    api.delete(`/projects/${id}`),
}

// ─── Quotes ─────────────────────────────────────────────────────────────────

export const quoteApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<Quote>>('/quotes', { params }),
  get: (id: number) =>
    api.get<Quote>(`/quotes/${id}`),
  create: (data: Partial<Quote>) =>
    api.post<Quote>('/quotes', data),
  update: (id: number, data: Partial<Quote>) =>
    api.put<Quote>(`/quotes/${id}`, data),
  delete: (id: number) =>
    api.delete(`/quotes/${id}`),
  stats: (params?: Record<string, unknown>) =>
    api.get<QuoteStats>('/quotes/stats', { params }),
  batches: () =>
    api.get<{ items: Array<{ batch_id: string; count: number; created_at: string | null; supplier_id: number | null; supplier_name: string; project_id: number | null; project_name: string }>; total: number }>('/quotes/batches'),
  deleteBatch: (batchId: string) =>
    api.delete(`/quotes/batches/${encodeURIComponent(batchId)}`),
  import: (formData: FormData) =>
    api.post<ImportResult>('/quotes/import', formData, {
      // Don't set Content-Type explicitly — axios will add the
      // required boundary when sending FormData if we leave it alone.
    }),
  batchConfirm: (data: {
    job_id: string
    supplier_id?: number
    supplier_name?: string
    project_id?: number
    project_name?: string
    category: string
    overrides?: Array<Record<string, unknown>>
    bid_status?: string
  }) => api.post<BatchConfirmResult>('/quotes/batch-confirm', data),
}

// ─── Intake (document upload + extraction polling) ──────────────────────────

export const intakeApi = {
  upload: (form: FormData, config?: AxiosRequestConfig) =>
    api.post<ExtractionJob>('/intake/upload', form, {
      // Don't set Content-Type explicitly — axios will add the
      // required boundary when sending FormData if we leave it alone.
      timeout: 60000,
      ...config,
    }),
  getJob: (jobId: string) =>
    api.get<ExtractionJob>(`/intake/jobs/${jobId}`),
  listJobs: (params?: Record<string, unknown>) =>
    api.get<{ items: ExtractionJob[]; total: number }>('/intake/jobs', { params }),
  enhance: (data: { job_id?: string; project_id?: number | null; items?: Array<Record<string, unknown>> }) =>
    api.post<EnhanceResponse>('/intake/enhance', data, { timeout: 180_000 }),
}

// ─── Analysis ───────────────────────────────────────────────────────────────

export const analysisApi = {
  dashboard: () =>
    api.get<DashboardSummary>('/analysis/dashboard'),
  heatmap: (params?: { date_from?: string; date_to?: string }) =>
    api.get<DashboardHeatmapData>('/analysis/dashboard/heatmap', { params }),
  bubble: (params?: { date_from?: string; date_to?: string }) =>
    api.get<DashboardBubbleData>('/analysis/dashboard/bubble', { params }),
  compare: (data: { category: string; sub_category?: string; new_price?: number; baseline_type?: string }) =>
    api.post<PriceCompareResult>('/analysis/compare', data),
  supplierScore: (data: { supplier_id: number; category?: string }) =>
    api.post<SupplierScore>('/analysis/supplier-score', data),
  multiCompare: (data: { supplier_ids: number[]; category: string; project_id?: number }) =>
    api.post<MultiCompareResult>('/analysis/multi-compare', data),
  bidMatrix: (data: { project_id?: number; supplier_ids: number[]; submission_ids?: number[]; material_ids?: number[]; category?: string }) =>
    api.post<BidMatrixResult>('/analysis/bid-matrix', data),
  bidInsight: (data: BidMatrixResult) =>
    api.post<BidInsight>('/analysis/bid-insight', data, { timeout: 60000 }),
  categoryStats: (category: string) =>
    api.get<CategoryDetailStats>(`/analysis/category-stats/${category}`),
  refreshBaselines: (category?: string) =>
    api.post('/analysis/refresh-baselines', null, { params: { category } }),
  // ── Bid Alignment ──
  alignmentSuggest: (data: {
    project_id?: number
    category: string
    supplier_ids: number[]
    rows: AlignmentRowInput[]
  }) =>
    api.post<AlignmentSuggestResult>('/analysis/bid-alignment/suggest', data, { timeout: 180_000 }),
  alignmentApply: (data: {
    project_id?: number
    category: string
    groups: AlignmentApplyGroup[]
    field_fixes: AlignmentApplyFieldFix[]
  }) =>
    api.post<AlignmentApplyResult>('/analysis/bid-alignment/apply', data),
  alignmentGroups: (params?: { project_id?: number; category?: string }) =>
    api.get<AlignmentGroupOut[]>('/analysis/bid-alignment/groups', { params }),
  alignmentDeleteGroup: (groupId: number) =>
    api.delete(`/analysis/bid-alignment/groups/${groupId}`),
  // ── Anchor / Tender-list ──
  tenderListPreview: (formData: FormData) =>
    api.post<TenderPreviewResult>('/analysis/tender-list/preview', formData),
  tenderListMatch: (formData: FormData) =>
    api.post<AnchorMatchSummary>('/analysis/tender-list/match', formData, { timeout: 180_000 }),
  tenderListLlmFill: (data: {
    project_id: number; category: string; supplier_ids?: number[];
    tender_list_session_id?: number | null; k?: number; mode?: string; model?: string | null
  }) =>
    api.post<LlmFillResult>('/analysis/tender-list/llm-fill', data, { timeout: 600_000 }),
  anchorReviewMatrix: (params: { project_id: number; category: string; supplier_ids?: string }) =>
    api.get<AnchorReviewMatrixResult>('/analysis/anchor-review/matrix', { params }),
  anchorReview: (params: { project_id: number; category: string; supplier_ids?: string }) =>
    api.get<AnchorReviewResult>('/analysis/anchor-review', { params }),
  anchorReviewConfirm: (data: { group_id: number; action: 'confirm' | 'reject' }) =>
    api.post('/analysis/anchor-review/confirm', data),
  anchorReviewItemConfirm: (data: { item_id: number; action: 'align' | 'exclude' }) =>
    api.post('/analysis/anchor-review/item-confirm', data),
  anchorReviewBulkConfirm: (params: { project_id: number; category: string }) =>
    api.post('/analysis/anchor-review/bulk-confirm', null, { params }),
  anchorReviewFinalize: (data: {
    project_id?: number; category: string; force?: boolean; reason?: string; finalized_by?: string
  }) =>
    api.post<{ ok: boolean; id: number; status: string; group_ids_count: number; pending_at_finalize: number }>('/analysis/anchor-review/finalize', data),
  tenderListConfirm: (data: {
    project_id?: number; category: string; file_name?: string; anchors_json?: unknown[]; anchors_total?: number; confirmed_by?: string; force?: boolean;
    source_type?: string; brand_requirement?: unknown[]; supplier_brands?: unknown[]
  }) =>
    api.post<{ ok: boolean; id: number; version: number; sessions: TenderListConfirmSession[]; multi_category: boolean }>('/analysis/tender-list/confirm', data),
  tenderListReconcile: (data: { xlsx_items: unknown[]; pdf_items: unknown[]; source_type?: string }) =>
    api.post<SourceReconcileResult>('/analysis/tender-list/reconcile', data),
  bidMatrixSave: (data: {
    project_id?: number; category: string; alignment_finalization_id: number;
    tender_list_session_id?: number; matrix_json?: object; readiness_json?: unknown[];
    anchors_count?: number; compared_rows?: number; excluded_rows_json?: unknown[];
    supplier_ids_json?: unknown[]; recommended_supplier?: string
  }) =>
    api.post<{ ok: boolean; id: number; version: number }>('/analysis/bid-matrix/save', data),
  bidMatrixVersionApprove: (versionId: number, data: { note?: string; approved_by?: string }) =>
    api.post<{ ok: boolean; id: number; status: string }>(`/analysis/bid-matrix/versions/${versionId}/approve`, data),
}

// ─── Config ─────────────────────────────────────────────────────────────────

export const configApi = {
  list: () =>
    api.get('/config'),
  get: (key: string) =>
    api.get(`/config/${key}`),
  update: (key: string, data: { value: Record<string, unknown>; description?: string }) =>
    api.put(`/config/${key}`, data),
}

// ─── Brand Tiers ─────────────────────────────────────────────────────────────

export const brandTierApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<BrandTier[]>('/brand-tiers', { params }),
  create: (data: Omit<BrandTier, 'id'>) =>
    api.post<BrandTier>('/brand-tiers', data),
  update: (id: number, data: Partial<BrandTier>) =>
    api.put<BrandTier>(`/brand-tiers/${id}`, data),
  delete: (id: number) =>
    api.delete(`/brand-tiers/${id}`),
}

// ─── Users ───────────────────────────────────────────────────────────────────

export const userApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<User>>('/users', { params }),
  create: (data: Omit<User, 'id' | 'last_login'>) =>
    api.post<User>('/users', data),
  update: (id: number, data: Partial<User>) =>
    api.put<User>(`/users/${id}`, data),
  delete: (id: number) =>
    api.delete(`/users/${id}`),
  toggleStatus: (id: number) =>
    api.patch<User>(`/users/${id}/status`),
}

// ─── Logs ────────────────────────────────────────────────────────────────────

export const logApi = {
  list: (params?: Record<string, unknown>) =>
    api.get<PaginatedResponse<LogEntry>>('/logs', { params }),
  export: (params?: Record<string, unknown>) =>
    api.get('/logs/export', { params, responseType: 'blob' }),
}

// ─── Invite (tender recommendation + persistence) ──────────────────────────

export const inviteApi = {
  recommend: (data: {
    tender_items: Array<Record<string, unknown>>
    top_n?: number
    project_id?: number
    brand_requirements?: string[]
  }) =>
    api.post<RecommendResponse>('/invite/recommend', data),
  save: (data: {
    tender_id?: number
    job_id?: string
    project_id?: number
    project_name?: string
    project_code?: string
    tender_date?: string
    deadline?: string
    items: Array<Record<string, unknown>>
    supplier_ids: number[]
  }) =>
    api.post<SaveInvitationsResponse>('/invite/save', data),
  listTenders: (params?: Record<string, unknown>) =>
    api.get<Array<Record<string, unknown>>>('/invite/tenders', { params }),
  getTender: (id: number) =>
    api.get<Record<string, unknown>>(`/invite/tenders/${id}`),
  // Legacy v1 interface — kept for compatibility, no backend implementation.
  recommendLegacy: (data: {
    project_name: string
    project_id?: number
    specs: { category: string; sub_category: string; quantity?: number; budget?: number }[]
  }) =>
    api.post<InviteResult>('/invite/recommend', data),
}

// ─── Export (Excel downloads) ────────────────────────────────────────────────

export const exportApi = {
  dashboard: () =>
    api.get('/export/dashboard', { responseType: 'blob' }),
  suppliers: () =>
    api.get('/export/suppliers', { responseType: 'blob' }),
  materials: (params?: { category?: string }) =>
    api.get('/export/materials', { params, responseType: 'blob' }),
  quotes: (params?: { category?: string; supplier_id?: number; project_id?: number; alert_level?: string }) =>
    api.get('/export/quotes', { params, responseType: 'blob' }),
  bidMatrix: (params: { supplier_ids: string; project_id?: number; category?: string }) =>
    api.get('/export/bid-matrix', { params, responseType: 'blob' }),
  logs: (params?: Record<string, unknown>) =>
    api.get('/export/logs', { params, responseType: 'blob' }),
}

// ─── OCR ─────────────────────────────────────────────────────────────────────

export const ocrApi = {
  parse: (formData: FormData) =>
    api.post<OcrResult>('/quotes/ocr', formData, {
      // Don't set Content-Type explicitly — axios will add the
      // required boundary when sending FormData if we leave it alone.
    }),
  confirm: (data: { items: OcrResult['items']; batch_id?: string }) =>
    api.post('/quotes/ocr/confirm', data),
}
