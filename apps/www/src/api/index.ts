import api from './client'
import type { AxiosRequestConfig } from 'axios'
import type {
  PaginatedResponse, Material, Supplier, Project, Quote,
  DashboardSummary, PriceCompareResult, SupplierScore,
  StandardizeResult, ExtendedAttrSchema, ImportResult,
  QuoteStats, CategoryDetailStats, MultiCompareResult,
  BidMatrixResult, BidMatrixPreviewResult, BidInsight, BrandTier, User, LogEntry,
  ExtractionJob, RecommendResponse, BatchConfirmResult,
  SaveInvitationsResponse,
  DashboardHeatmapData, DashboardBubbleData,
  EnhanceResponse,
  AnchorMatchSummary, AnchorReviewResult, AnchorReviewMatrixResult, TenderPreviewResult,
  TenderListConfirmSession, TenderListCurrentSession, SourceReconcileResult,
  CompareStateResult, ClassifyTier0Result, SummarizeFactsResult,
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
  /** 系统支持的品类词表（与主数据里有没有物料无关）——品类选择器的选项来源。
   *  不在前端另抄一份，否则迟早跟后端 `PROFESSION_MAP` 漂移。 */
  supportedCategories: () =>
    api.get<string[]>('/materials/supported-categories'),
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
  /** 按 (name, code) 精确找已有项目；没有返回 null。见后端 find_project_exact。 */
  findExact: (name: string, code: string) =>
    api.get<Project | null>('/projects/find-exact', { params: { name, code } }),
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
  // 软删除单条比价暂存 submission（标记 superseded，可复活）
  supersedeSubmission: (submissionId: number) =>
    api.delete<{ submission_id: number; status: string; already: boolean }>(
      `/quotes/submissions/${submissionId}`,
    ),
  // 一键移除：软删除某项目下全部 active submission
  supersedeProjectSubmissions: (projectId: number) =>
    api.delete<{ superseded_ids: number[]; count: number }>('/quotes/submissions', {
      params: { project_id: projectId },
    }),
  // 移除在途/失败的识别任务（无 submission）：标记 job lifecycle=removed
  removeJob: (jobId: string) =>
    api.delete<{ job_id: string; lifecycle: string; already: boolean }>(
      `/quotes/jobs/${jobId}`,
    ),
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
    // 评审 R2（第3块）：declared_total_mismatch 结构化错误的放行开关——
    // 此前前端根本没有这个参数，用户永远无法在核对过差异后强制入库。
    checksum_ack?: boolean
    // design/24 B3：预演——跑一遍完全相同的判据、从不写库，一次性返回这份
    // 文档所有的结构性疑点。收件箱（design/24 后续阶段）用它做"进收件箱前
    // 预检"；本轮只接后端能力，UI 消费方留给前端 Stage 组件那一步接。
    dry_run?: boolean
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
  // 全局默认 15s 对这个接口太紧：它本身只是一次主键读，但多份扫描件同时识别
  // 时，识别线程占着 GIL 和 pdfium 锁，这次读要排很久才轮得到 —— 实测服务端
  // 每一次都返回了 200，是客户端先放弃的（design/29 §16）。读一行数据而已，
  // 超时给宽一点不花任何代价。
  getJob: (jobId: string) =>
    api.get<ExtractionJob>(`/intake/jobs/${jobId}`, { timeout: 60_000 }),
  listJobs: (params?: Record<string, unknown>) =>
    api.get<{ items: ExtractionJob[]; total: number }>('/intake/jobs', { params }),
  enhance: (data: { job_id?: string; project_id?: number | null; items?: Array<Record<string, unknown>> }) =>
    api.post<EnhanceResponse>('/intake/enhance', data, { timeout: 180_000 }),
  // design/28 §3 Tier 0——瞬时判定，不建 job，cut 5 拖拽确认屏用它给每份
  // 刚拖进来的文件一个初步判定，不等真正上传/识别。
  // 超时 90s（原 30s）：扫描件分类要渲染前几页原生分辨率图 + 一次视觉调用，
  // 单份实测 6.5-9s，但同进程里若有招标文件正在识别，pdfium 渲染要在
  // `_PDF_LOCK` 上排队（design/29 §12.1 实测：四份并发时最后一份超过 30s）。
  // 客户端已改成逐个送分类请求，这里再留一档余量，避免"后端 200、前端已放弃"。
  classifyTier0: (form: FormData) =>
    api.post<ClassifyTier0Result>('/intake/classify-tier0', form, { timeout: 90_000 }),
  // design/29 §4——工作台卡片概述。facts 只传已确认的结构化字段。
  summarizeFacts: (kind: 'tender' | 'bid', facts: Record<string, unknown>) =>
    api.post<SummarizeFactsResult>('/intake/summarize-facts', { kind, facts }, { timeout: 15_000 }),
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
  // design/31 cut 2b：先比价、后逐行确认。跑的是官方链路本身，只是不落库；
  // 入参跟 batchConfirm 同形状，因为预览与正式吃的就是同一份输入。沙箱里要
  // 串完"入库→对齐→矩阵"，比单份 dry-run 慢得多，超时给足。
  bidMatrixPreview: (data: {
    project_id: number
    category: string
    confirmations: Array<Record<string, unknown>>
  }) => api.post<BidMatrixPreviewResult>('/analysis/bid-matrix/preview', data, { timeout: 180_000 }),
  bidInsight: (data: BidMatrixResult) =>
    api.post<BidInsight>('/analysis/bid-insight', data, { timeout: 60000 }),
  categoryStats: (category: string) =>
    api.get<CategoryDetailStats>(`/analysis/category-stats/${category}`),
  refreshBaselines: (category?: string) =>
    api.post('/analysis/refresh-baselines', null, { params: { category } }),
  // R1 止血：bid-alignment/suggest·apply·groups·groups/{id} 这一整组（旧的
  // "AI 建议对齐"流程）已被 anchor-review/matrix 那套取代，四个 wrapper
  // 零调用方，整组删除，不留半截。
  // ── Anchor / Tender-list ──
  tenderListPreview: (formData: FormData) =>
    api.post<TenderPreviewResult>('/analysis/tender-list/preview', formData),
  tenderListMatch: (formData: FormData) =>
    api.post<AnchorMatchSummary>('/analysis/tender-list/match', formData, { timeout: 180_000 }),
  // tenderListLlmFill：零调用方（评审 E4 Tier 2 时已核实：功能完整但从未接
  // 入 UI，见 docs/design/22）。wrapper 一并删除，恢复入口留给产品决策。
  anchorReviewMatrix: (params: { project_id: number; category: string; submission_ids?: string; supplier_ids?: string }) =>
    api.get<AnchorReviewMatrixResult>('/analysis/anchor-review/matrix', { params }),
  anchorReview: (params: { project_id: number; category: string; submission_ids?: string; supplier_ids?: string }) =>
    api.get<AnchorReviewResult>('/analysis/anchor-review', { params }),
  // anchorReviewConfirm / anchorReviewBulkConfirm：零调用方（group 级批量
  // 确认从未接入 UI，复核矩阵页面走的是 anchorReviewItemConfirm 逐项确认）。
  anchorReviewItemConfirm: (data: { item_id: number; action: 'align' | 'exclude' }) =>
    api.post('/analysis/anchor-review/item-confirm', data),
  // design/23：复核者确认"这格确实无报价，符合预期"；acked:false 撤销确认。
  anchorReviewMissingAck: (data: {
    project_id: number; category: string; anchor_seq: string; submission_id: number; acked: boolean
  }) =>
    api.post<{ ok: boolean; anchor_seq: string; submission_id: number; acked: boolean }>(
      '/analysis/anchor-review/missing-ack', data,
    ),
  anchorReviewFinalize: (data: {
    project_id?: number; category: string; force?: boolean; reason?: string; finalized_by?: string
  }) =>
    api.post<{ ok: boolean; id: number; status: string; group_ids_count: number; pending_at_finalize: number }>('/analysis/anchor-review/finalize', data),
  tenderListConfirm: (data: {
    project_id?: number; category: string; file_name?: string; anchors_json?: unknown[]; anchors_total?: number; confirmed_by?: string; force?: boolean;
    source_type?: string; brand_requirement?: unknown[]; supplier_brands?: unknown[]
  }) =>
    api.post<{ ok: boolean; id: number; version: number; primary_category: string; sessions: TenderListConfirmSession[]; multi_category: boolean }>('/analysis/tender-list/confirm', data),
  tenderListCurrentSessions: (params: { project_id: number }) =>
    api.get<{ sessions: TenderListCurrentSession[]; primary_category: string }>('/analysis/tender-list/current-sessions', { params }),
  compareState: (params: { project_id: number }) =>
    api.get<CompareStateResult>('/analysis/compare-state', { params }),
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

// ─── Auth ───────────────────────────────────────────────────────────────────

export const authApi = {
  me: () =>
    api.get<{
      id: number
      username: string
      nickname: string
      role: string
      email: string
      phone: string
      status: string
    }>('/auth/me'),
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
    supplier_ids?: number[]
    brand_requirements?: string[]
  }) =>
    api.post<SaveInvitationsResponse>('/invite/save', data),
  listTenders: (params?: Record<string, unknown>) =>
    api.get<Array<Record<string, unknown>>>('/invite/tenders', { params }),
  getTender: (id: number) =>
    api.get<Record<string, unknown>>(`/invite/tenders/${id}`),
  // R1 止血：recommendLegacy 已删除。原注释说"no backend implementation"是
  // 过时的——POST /invite/recommend 在 apps/api/routes/invite.py 里其实是
  // 真实实现的路由，只是前端零调用方（当前邀标推荐走的是别的流程）。删的是
  // 前端死 wrapper，不代表后端路由本身也该删——未核实是否有其他调用方
  // （脚本/外部集成），不在这轮动它。
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

// R1 止血：ocrApi 已删除 —— 指向的 /quotes/ocr、/quotes/ocr/confirm 两条路由
// 在后端已不存在（grep apps/api/routes 零命中），调用必 404；前端也早已零
// 调用方，是纯粹的死 wrapper。当前 OCR 增强上传走 intakeApi + ExtractionEditor，
// 不是这套。
