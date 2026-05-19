import api from './client'
import type {
  PaginatedResponse, Material, Supplier, Project, Quote,
  DashboardSummary, PriceCompareResult, SupplierScore,
  StandardizeResult, ExtendedAttrSchema, ImportResult,
  QuoteStats, CategoryDetailStats, MultiCompareResult,
  BidMatrixResult, BrandTier, User, LogEntry,
  InviteResult, OcrResult,
  ExtractionJob, RecommendResponse, BatchConfirmResult,
  SaveInvitationsResponse,
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
  import: (formData: FormData) =>
    api.post<ImportResult>('/quotes/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  batchConfirm: (data: {
    job_id: string
    supplier_id?: number
    supplier_name?: string
    project_id?: number
    project_name?: string
    category: string
    overrides?: Array<Record<string, unknown>>
  }) => api.post<BatchConfirmResult>('/quotes/batch-confirm', data),
}

// ─── Intake (document upload + extraction polling) ──────────────────────────

export const intakeApi = {
  upload: (form: FormData) =>
    api.post<ExtractionJob>('/intake/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    }),
  getJob: (jobId: string) =>
    api.get<ExtractionJob>(`/intake/jobs/${jobId}`),
  listJobs: (params?: Record<string, unknown>) =>
    api.get<{ items: ExtractionJob[]; total: number }>('/intake/jobs', { params }),
}

// ─── Analysis ───────────────────────────────────────────────────────────────

export const analysisApi = {
  dashboard: () =>
    api.get<DashboardSummary>('/analysis/dashboard'),
  compare: (data: { category: string; sub_category?: string; new_price?: number; baseline_type?: string }) =>
    api.post<PriceCompareResult>('/analysis/compare', data),
  supplierScore: (data: { supplier_id: number; category?: string }) =>
    api.post<SupplierScore>('/analysis/supplier-score', data),
  multiCompare: (data: { supplier_ids: number[]; category: string; project_id?: number }) =>
    api.post<MultiCompareResult>('/analysis/multi-compare', data),
  bidMatrix: (data: { project_id?: number; supplier_ids: number[]; material_ids?: number[] }) =>
    api.post<BidMatrixResult>('/analysis/bid-matrix', data),
  categoryStats: (category: string) =>
    api.get<CategoryDetailStats>(`/analysis/category-stats/${category}`),
  refreshBaselines: (category?: string) =>
    api.post('/analysis/refresh-baselines', null, { params: { category } }),
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
    api.post(`/users/${id}/toggle-status`),
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

// ─── OCR ─────────────────────────────────────────────────────────────────────

export const ocrApi = {
  parse: (formData: FormData) =>
    api.post<OcrResult>('/quotes/ocr', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  confirm: (data: { items: OcrResult['items']; batch_id?: string }) =>
    api.post('/quotes/ocr/confirm', data),
}
