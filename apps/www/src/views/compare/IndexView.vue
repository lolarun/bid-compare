<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  CheckCircleOutlined, LineChartOutlined, RightOutlined, LeftOutlined,
  CloudUploadOutlined, LoadingOutlined, CheckOutlined, CloseCircleOutlined,
  PlusOutlined,
  AppstoreOutlined, TeamOutlined, TrophyOutlined, DollarOutlined,
  WarningOutlined, BulbOutlined, RobotOutlined,
  FilePdfOutlined, FileExcelOutlined, InboxOutlined,
} from '@ant-design/icons-vue'
import { projectApi, supplierApi, analysisApi, quoteApi, intakeApi } from '@/api'
import type {
  Project,
  Supplier,
  BidMatrixResult,
  BidInsight,
  ExtractionJob,
  QuoteExtractionItem,
  BatchConfirmResult,
  AnchorMatchSummary,
  AnchorReviewResult,
  TenderPreviewResult,
  TenderBidlistResult,
  TenderBrandReq,
  TenderSupplierBrand,
  SourceReconcileResult,
  PageDiagnostic,
  PdfQualityMetrics,
} from '@/api/client'
import IntakeUploader from '@/components/IntakeUploader.vue'
import ExtractionEditor from '@/components/ExtractionEditor.vue'
import StatCard from '@/components/StatCard.vue'
import BidMatrix from './components/BidMatrix.vue'
import AnchorReviewMatrix from './components/AnchorReviewMatrix.vue'
import { normalizeAlert, formatDeviation } from '@/utils/alert'
import { asQuoteShape } from '@/utils/extraction'

// Steps: 0=config, 1=procurement list, 2=supplier quotes, 3=alignment review, 4=matrix
const STEP_RESULTS = 4

// ─── State ───────────────────────────────────────────────────────────────
const route = useRoute()
const router = useRouter()
const currentStep = ref(0)
const selectedProfession = ref<string | undefined>(undefined)

const taskConfig = reactive<{
  projectId: number | undefined
  category: string
  supplierIds: number[]
  bidStatus: string
}>({
  projectId: undefined,
  category: '',
  supplierIds: [],
  bidStatus: '',
})

watch(selectedProfession, () => {
  taskConfig.category = ''
})

const projects = ref<Project[]>([])
const allSuppliers = ref<Supplier[]>([])

// ─── Step 1: Procurement list preview ────────────────────────────────────
const tenderFile = ref<File | null>(null)
const tenderPreview = ref<TenderPreviewResult | null>(null)
const tenderPreviewing = ref(false)
const tenderCategory = ref('')
const confirmedCategories = ref<string[]>([])   // 已确认的品类(多品类拆分后驱动切换器)
const categorySessionMap = ref<Record<string, number>>({})  // {品类 → session_id}
const forceUnknownCategory = ref(false)  // 用户显式确认强制归入
const tenderListConfirming = ref(false)
const tenderListSessionId = ref<number | null>(null)
const categoryExplicitlySelected = ref(false)  // 多品类场景：用户显式点击品类切换器后才允许入库

// ─── Step 1 (招标 PDF)：异步抽取 + 品牌映射 + 页范围 + 对账 ────────────────
// PDF 招标清单是比价主来源，Excel 仅作对照参考
const tenderPdfFile = ref<File | null>(null)
const tenderBrandRequirement = ref<TenderBrandReq[]>([])
const tenderSupplierBrands = ref<TenderSupplierBrand[]>([])
const tenderDetectedPages = ref<{ bidlist: number[]; brand: number | null } | null>(null)
const tenderJobStage = ref('')
const tenderJobPct = ref(0)
const tenderJobError = ref('')
const overrideBidlistPages = ref('')   // 用户可手动修正，如 "14-18" 或 "14,15,16"
const overrideBrandPage = ref('')      // 如 "13"
let tenderPollTimer: ReturnType<typeof setInterval> | null = null

// PDF 补充结果（品牌+材质）+ 对账状态
const pdfSupplement = ref<TenderBidlistResult | null>(null)
const showExcelPanel = ref(false)       // 展开/收起 Excel 对照参考面板
const reconcileResult = ref<SourceReconcileResult | null>(null)
const reconcileLoading = ref(false)
const reconcileConfirmed = ref(false)   // 用户已确认差异

function _parsePages(s: string): number[] | undefined {
  const t = (s || '').trim()
  if (!t) return undefined
  const out: number[] = []
  for (const part of t.split(/[,，]/)) {
    const seg = part.trim()
    const m = seg.match(/^(\d+)\s*[-~～]\s*(\d+)$/)
    if (m) { for (let i = +m[1]; i <= +m[2]; i++) out.push(i) }
    else if (/^\d+$/.test(seg)) out.push(+seg)
  }
  return out.length ? out : undefined
}

function stopTenderPoll() {
  if (tenderPollTimer) { clearInterval(tenderPollTimer); tenderPollTimer = null }
}

// ── Excel 主清单预览 ───────────────────────────────────────────────────────
async function previewTenderList(file: File) {
  tenderPreviewing.value = true
  tenderPreview.value = null
  tenderJobError.value = ''
  try {
    const form = new FormData()
    form.append('file', file)
    const { data } = await analysisApi.tenderListPreview(form)
    tenderFile.value = file
    tenderPreview.value = data
    tenderCategory.value = data.detected_category
    // Reset PDF supplement + reconcile when Excel changes
    pdfSupplement.value = null
    tenderBrandRequirement.value = []
    tenderSupplierBrands.value = []
    tenderDetectedPages.value = null
    tenderPdfFile.value = null
    reconcileResult.value = null
    reconcileConfirmed.value = false
    showExcelPanel.value = true   // 上传 Excel 后自动展开参考面板
    message.success(`参考清单已预览：${data.total} 条采购项`)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? (e as Error).message
    tenderJobError.value = `预览失败：${detail}`
    message.error(tenderJobError.value)
  } finally {
    tenderPreviewing.value = false
  }
}

// ── PDF 品牌补充：上传 + 轮询 ─────────────────────────────────────────────
async function uploadTenderPdf(file: File) {
  tenderPdfFile.value = file
  overrideBidlistPages.value = ''
  overrideBrandPage.value = ''
  await runTenderExtract(file, {})
}

async function reExtractTenderPdf() {
  if (!tenderPdfFile.value) return
  const ctx: Record<string, unknown> = {}
  const bl = _parsePages(overrideBidlistPages.value)
  const bp = _parsePages(overrideBrandPage.value)
  if (bl) ctx.bidlist_pages = bl
  if (bp && bp.length) ctx.brand_page = bp[0]
  await runTenderExtract(tenderPdfFile.value, ctx)
}

async function runTenderExtract(file: File, ctx: Record<string, unknown>) {
  stopTenderPoll()
  tenderPreviewing.value = true
  pdfSupplement.value = null   // clear previous PDF result only
  tenderJobError.value = ''
  tenderJobStage.value = '上传中'
  tenderJobPct.value = 3
  const form = new FormData()
  form.append('file', file)
  form.append('type', 'tender_bidlist')
  if (taskConfig.projectId) form.append('project_id', String(taskConfig.projectId))
  if (Object.keys(ctx).length) form.append('context_json', JSON.stringify(ctx))
  try {
    const { data } = await intakeApi.upload(form)
    if (data.status === 'done') onTenderJobDone(data)
    else if (data.status === 'failed') {
      tenderPreviewing.value = false
      tenderJobError.value = data.error || '识别失败'
      message.error(tenderJobError.value)
    } else startTenderPoll(data.id)
  } catch (e: unknown) {
    tenderPreviewing.value = false
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? (e as Error).message
    tenderJobError.value = `上传失败：${detail}`
    message.error(tenderJobError.value)
  }
}

function startTenderPoll(jobId: string) {
  stopTenderPoll()
  tenderPollTimer = setInterval(async () => {
    try {
      const { data } = await intakeApi.getJob(jobId)
      tenderJobStage.value = data.progress_stage || ''
      tenderJobPct.value = data.progress_pct ?? 0
      if (data.status === 'done') { stopTenderPoll(); onTenderJobDone(data) }
      else if (data.status === 'failed') {
        stopTenderPoll()
        tenderPreviewing.value = false
        tenderJobError.value = data.error || '识别失败'
        message.error(tenderJobError.value)
      }
    } catch { /* transient poll error — keep trying */ }
  }, 2000)
}

function onTenderJobDone(job: ExtractionJob) {
  tenderPreviewing.value = false
  const r = job.result as unknown as TenderBidlistResult | null
  if (!r || !Array.isArray(r.items) || r.items.length === 0) {
    tenderJobError.value = '识别结果为空，请确认页范围或重新上传'
    message.error(tenderJobError.value)
    return
  }
  // PDF 是主来源 — 更新品牌/材质映射；Excel 仅作对照参考
  pdfSupplement.value = r
  tenderBrandRequirement.value = r.brand_requirement || []
  tenderSupplierBrands.value = r.supplier_brands || []
  tenderDetectedPages.value = r.detected_pages || null
  showPageOverride.value = false
  showQualityDetails.value = false
  excelOnlyItemActions.value = {}
  const bl = r.detected_pages?.bidlist || []
  if (bl.length) {
    overrideBidlistPages.value = bl.length > 1 ? `${bl[0]}-${bl[bl.length - 1]}` : String(bl[0])
  }
  if (r.detected_pages?.brand) overrideBrandPage.value = String(r.detected_pages.brand)
  // 自动识别品类（PDF 是主来源，始终覆盖）
  if (r.detected_category) {
    tenderCategory.value = r.detected_category
  }
  message.success(`招标 PDF 已解析：${r.row_count} 行主清单，第 ${r.detected_pages?.brand ?? '?'} 页品牌表`)
  // 如果 Excel 已加载，自动对账
  if (tenderPreview.value && r.items.length > 0) {
    runReconcile()
  }
}

async function runReconcile() {
  if (!tenderPreview.value || !pdfSupplement.value) return
  reconcileLoading.value = true
  reconcileResult.value = null
  reconcileConfirmed.value = false
  try {
    const { data } = await analysisApi.tenderListReconcile({
      xlsx_items: tenderPreview.value.items as unknown[],
      pdf_items: pdfSupplement.value.items as unknown[],
      source_type: pdfSupplement.value.source_type ?? 'excel_primary',
    })
    reconcileResult.value = data
    if (data.recommended_source === 'both_consistent') {
      message.success('PDF 与 Excel 清单行项目一致')
    } else if (data.recommended_source === 'pdf') {
      const excelOnly = (data.only_in_excel_reference ?? []).length
      const mismatches = data.field_mismatches.length
      message.info(`PDF 主清单（${data.pdf_count} 条），Excel 参考差异：${excelOnly} 条独有行，${mismatches} 处字段不符`)
    } else {
      const missing = data.seq_missing_in_pdf.length + data.seq_missing_in_xlsx.length
      const mismatches = data.field_mismatches.length
      message.warning(`发现差异：${missing} 条序号缺失，${mismatches} 处字段不符，请确认后继续`)
    }
  } catch {
    message.error('对账请求失败')
  } finally {
    reconcileLoading.value = false
  }
}

// ─── PDF quality helpers ──────────────────────────────────────────────────
function pct(n: number | undefined | null): string {
  if (n == null) return '—'
  return (n * 100).toFixed(0) + '%'
}

// Computed shorthand so template stays readable
const pdfQm = computed((): PdfQualityMetrics | null => pdfSupplement.value?.quality_metrics ?? null)
const pdfDiagnostics = computed((): PageDiagnostic[] => pdfSupplement.value?.page_diagnostics ?? [])

// ── Step 2 UI state ───────────────────────────────────────────────────────
const showPageOverride = ref(false)
const showQualityDetails = ref(false)
const excelOnlyItemActions = ref<Record<string, 'ignore' | 'add' | 'pending'>>({})

interface ExcelDiffItem {
  seq: string | number
  name?: string
  spec?: string
  unit?: string
  qty?: number | null
  [key: string]: unknown
}

const excelOnlyItems = computed((): ExcelDiffItem[] => {
  const seqs = reconcileResult.value?.only_in_excel_reference ?? []
  if (!seqs.length || !tenderPreview.value) return []
  const seqSet = new Set(seqs.map(String))
  return (tenderPreview.value.items as unknown as ExcelDiffItem[]).filter(
    item => seqSet.has(String(item.seq))
  )
})

const pdfConclusionStatus = computed((): 'ok' | 'warning' | 'error' => {
  if (!pdfSupplement.value) return 'ok'
  const qm = pdfQm.value
  if (qm && (qm.seq_missing.length > 5 || (qm.qty_parse_success_rate != null && qm.qty_parse_success_rate < 0.5))) return 'error'
  if ((qm?.seq_missing.length ?? 0) > 0 || (qm?.seq_duplicate.length ?? 0) > 0) return 'warning'
  if ((reconcileResult.value?.only_in_excel_reference ?? []).length > 0) return 'warning'
  if ((reconcileResult.value?.field_mismatches ?? []).length > 0) return 'warning'
  return 'ok'
})

const pdfConclusionTitle = computed((): string => {
  if (!pdfSupplement.value) return ''
  if (pdfConclusionStatus.value === 'error') return 'PDF识别质量较低，建议检查后重新上传'
  const excelOnly = (reconcileResult.value?.only_in_excel_reference ?? []).length
  if (excelOnly > 0) return 'PDF主清单已识别，发现Excel参考差异'
  if ((pdfQm.value?.seq_missing.length ?? 0) > 0) return 'PDF主清单已识别，部分序号待核查'
  return 'PDF主清单已识别，可继续'
})

const pdfConclusionDesc = computed((): string => {
  const r = pdfSupplement.value
  if (!r) return ''
  return `${r.row_count} 项采购项 · 清单页 ${overrideBidlistPages.value || '?'} · 品牌表第 ${tenderDetectedPages.value?.brand ?? '?'} 页`
})

const pdfDiffSummary = computed((): string => {
  const excelOnly = (reconcileResult.value?.only_in_excel_reference ?? []).length
  const mismatches = (reconcileResult.value?.field_mismatches ?? []).length
  if (!excelOnly && !mismatches) return ''
  const parts: string[] = []
  if (excelOnly) parts.push(`${excelOnly} 项仅在采购清单Excel中`)
  if (mismatches) parts.push(`${mismatches} 处字段存在差异`)
  return parts.join(' · ') + '，不影响PDF主清单比价'
})

function setExcelItemAction(seq: string, action: 'ignore' | 'add' | 'pending') {
  excelOnlyItemActions.value[seq] = action
}
function clearExcelItemAction(seq: string) {
  delete excelOnlyItemActions.value[seq]
}

// ─── Step 3: Alignment finalization gate ─────────────────────────────────
const alignmentFinalizing = ref(false)
const alignmentFinalizationId = ref<number | null>(null)

// ─── Step 4: Matrix save / approve gate ──────────────────────────────────
const matrixSaving = ref(false)
const savedMatrixVersionId = ref<number | null>(null)
const matrixApproving = ref(false)
const matrixApproved = ref(false)

async function confirmTenderListVersion() {
  const isPdfPrimary = !!pdfSupplement.value
  if (!isPdfPrimary && !tenderPreview.value) return
  // unknown_count check only meaningful for Excel (PDF items come pre-categorised)
  const hasUnknown = !isPdfPrimary && (tenderPreview.value?.unknown_count ?? 0) > 0
  if (hasUnknown && !forceUnknownCategory.value) {
    message.warning('存在未识别品类的采购项，请勾选「强制归入默认品类」后再确认')
    return
  }
  tenderListConfirming.value = true
  try {
    const { data } = await analysisApi.tenderListConfirm({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
      file_name: isPdfPrimary ? (tenderPdfFile.value?.name ?? '') : (tenderFile.value?.name ?? ''),
      anchors_total: isPdfPrimary ? pdfSupplement.value!.row_count : tenderPreview.value!.total,
      anchors_json: isPdfPrimary ? pdfSupplement.value!.items : tenderPreview.value!.items,
      force: hasUnknown && forceUnknownCategory.value,
      source_type: isPdfPrimary ? 'pdf_primary' : 'excel',
      brand_requirement: tenderBrandRequirement.value,
      supplier_brands: tenderSupplierBrands.value,
    })
    // ── 品类状态同步：顺序不可乱，tenderCategory 必须先于 tenderListSessionId 赋值 ──
    confirmedCategories.value = (data.sessions || []).map(s => s.category)
    categorySessionMap.value = Object.fromEntries(
      (data.sessions || []).map(s => [s.category, s.id])
    )
    // primary_category 由服务端确定（锚点最多的品类）；前端取 data.primary_category 为准
    const primaryCat: string = data.primary_category
      || data.sessions?.find(s => s.id === data.id)?.category
      || confirmedCategories.value[0]
      || ''
    tenderCategory.value = primaryCat
    taskConfig.category = primaryCat
    tenderListSessionId.value = categorySessionMap.value[primaryCat] ?? data.id
    // 单品类时视为已显式选择；多品类时用户须手动点选后方可入库
    categoryExplicitlySelected.value = confirmedCategories.value.length === 1
    if (data.multi_category) {
      const parts = data.sessions.map(s => `${s.category}(${s.anchors_total})`).join('、')
      message.success(`已按品类拆分为 ${data.sessions.length} 份采购清单：${parts}`)
    } else {
      message.success(`采购清单版本 v${data.version} 已确认`)
    }
    forceUnknownCategory.value = false
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (detail && typeof detail === 'object' && (detail as Record<string, unknown>).error === 'unknown_categories') {
      const d = detail as { unknown_count: number; unknown_items: string[] }
      message.error(`${d.unknown_count} 项品类未识别（${d.unknown_items.slice(0, 3).join('、')}…），请核对后勾选强制归入`)
    } else {
      message.error('确认失败，请重试')
    }
  } finally {
    tenderListConfirming.value = false
  }
}

async function finalizeAlignment() {
  alignmentFinalizing.value = true
  try {
    const { data } = await analysisApi.anchorReviewFinalize({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
    })
    alignmentFinalizationId.value = data.id
    message.success(`对齐审核已完成 (${data.group_ids_count} 组已锁定)`)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail ?? '完成对齐审核失败')
  } finally {
    alignmentFinalizing.value = false
  }
}

async function saveMatrixVersion() {
  if (alignmentFinalizationId.value === null) {
    message.warning('请先完成对齐审核，再保存比价版本')
    return
  }
  matrixSaving.value = true
  try {
    const { data } = await analysisApi.bidMatrixSave({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
      alignment_finalization_id: alignmentFinalizationId.value,
      tender_list_session_id: tenderListSessionId.value ?? undefined,
      supplier_ids_json: effectiveSupplierIds.value,
      recommended_supplier: matrixSummary.value?.price_preferred_candidate?.name ?? '',
    })
    savedMatrixVersionId.value = data.id
    message.success(`比价版本 v${data.version} 已保存`)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail ?? '保存失败')
  } finally {
    matrixSaving.value = false
  }
}

async function approveMatrixVersion() {
  if (!savedMatrixVersionId.value) return
  matrixApproving.value = true
  try {
    await analysisApi.bidMatrixVersionApprove(savedMatrixVersionId.value, {})
    matrixApproved.value = true
    message.success('比价已审批通过')
  } catch {
    message.error('审批失败，请重试')
  } finally {
    matrixApproving.value = false
  }
}

// ─── Step 3: Pending item gate ────────────────────────────────────────────
const pendingItemLoading = ref<Record<number, boolean>>({})
const matchRunning = ref(false)

// Pending count is now owned by AnchorReviewMatrix component
const reviewPendingCount = ref<number | null>(null)

const allPendingActioned = computed(() => {
  // Use new matrix count if available, fall back to legacy
  if (reviewPendingCount.value !== null) return reviewPendingCount.value === 0
  if (!anchorReviewResult.value) return false
  return (anchorReviewResult.value.pending_items_total ?? anchorReviewResult.value.low_conf_groups.length) === 0
})

/** Item-level: confirm single pending item into matrix or exclude it */
async function confirmPendingItem(itemId: number, action: 'align' | 'exclude') {
  pendingItemLoading.value[itemId] = true
  try {
    await analysisApi.anchorReviewItemConfirm({ item_id: itemId, action })
    await loadAnchorReview()
    message.success(action === 'align' ? '已确认归入矩阵' : '已排除')
  } catch {
    message.error('操作失败，请重试')
  } finally {
    pendingItemLoading.value[itemId] = false
  }
}


// Per-supplier upload state for Step 2 (legacy slot mode)
const supplierUploads = reactive<Record<number, {
  job: ExtractionJob | null
  items: QuoteExtractionItem[]
  confirmed: boolean
  batch_id?: string
  unknown_brands: string[]
}>>({})

// ─── Batch upload state (new flow) ─────────────────────────────────────
interface BatchFileEntry {
  id: string           // unique key
  filename: string
  status: 'uploading' | 'processing' | 'done' | 'failed'
  stage: string
  progressPct: number
  uploadPct: number
  jobId: string | null
  detectedSupplierName: string   // OCR-detected name (read-only source of truth)
  finalSupplierName: string      // user-editable display name (always takes precedence)
  matchedSupplierId: number | null  // set when user selects from dropdown; null = stranger
  items: QuoteExtractionItem[]
  confirmedSupplierId: number | null    // null for unknown suppliers
  confirmedSubmissionId: number | null  // always set on confirm success
  confirmed: boolean
  error: string
  pollTimer: ReturnType<typeof setInterval> | null
}
const batchFiles = ref<BatchFileEntry[]>([])
const useBatchMode = computed(() => taskConfig.supplierIds.length === 0)

const batchProgress = computed(() => {
  const total = batchFiles.value.length
  if (total === 0) return null
  const done = batchFiles.value.filter((f) => f.status === 'done' || f.confirmed).length
  const failed = batchFiles.value.filter((f) => f.status === 'failed').length
  const processing = total - done - failed
  return { total, done, failed, processing }
})

const BATCH_PROGRESS_STEPS = [
  { key: 'upload', label: '上传', pct: 1 },
  { key: 'received', label: '已接收', pct: 5 },
  { key: 'render', label: '渲染 PDF', pct: 10 },
  { key: 'split', label: '拆分页面', pct: 15 },
  { key: 'recognize', label: '逐页识别', pct: 20 },
  { key: 'merge', label: '合并结果', pct: 88 },
  { key: 'cleanup', label: '整理结果', pct: 95 },
  { key: 'done', label: '已识别', pct: 100 },
] as const

// Bid matrix result (Step 4)
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)

// New-project modal
const newProjectVisible = ref(false)
const newProjectSaving = ref(false)
const newProjectForm = reactive({
  name: '',
  code: '',
  location: '',
  remark: '',
})
function openNewProjectModal() {
  Object.assign(newProjectForm, { name: '', code: '', location: '', remark: '' })
  newProjectVisible.value = true
}
async function handleCreateProject() {
  if (!newProjectForm.name.trim()) {
    message.warning('请输入项目名称')
    return
  }
  newProjectSaving.value = true
  try {
    const { data } = await projectApi.create({
      name: newProjectForm.name.trim(),
      code: newProjectForm.code.trim(),
      location: newProjectForm.location.trim(),
      remark: newProjectForm.remark.trim(),
    })
    message.success('项目创建成功')
    await fetchProjects()
    taskConfig.projectId = data.id
    newProjectVisible.value = false
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '创建失败')
  } finally {
    newProjectSaving.value = false
  }
}

// ─── Computed ────────────────────────────────────────────────────────────
const canProceedFromConfig = computed(() => !!taskConfig.projectId)

const canProceedFromUpload = computed(() => {
  if (useBatchMode.value) {
    return batchFiles.value.filter((f) => f.confirmed).length >= 1
  }
  return taskConfig.supplierIds.every((sid) => supplierUploads[sid]?.confirmed === true)
})

const selectedSuppliers = computed(() =>
  allSuppliers.value.filter((s) => taskConfig.supplierIds.includes(s.id))
)
const selectedProjectName = computed(() =>
  projects.value.find((p) => p.id === taskConfig.projectId)?.name || ''
)

const matrixRows = computed(() => matrixResult.value?.rows ?? [])
const matrixTotals = computed(() => matrixResult.value?.totals ?? [])
const matrixSuppliers = computed(() => matrixResult.value?.suppliers ?? [])
const matrixSummary = computed(() => {
  if (!matrixResult.value) return null
  // B3：matrixResult 现在完整声明了 recommendation_level/award_mode/
  // committee_required/price_ranking/risks/price_preferred_candidate 等字段
  // （此前靠 Record<string,any> 旁路读取，此处一并接掉这个逃生舱）。
  const m = matrixResult.value
  const rows = m.rows
  const suppliers = m.suppliers
  // 招标文件驱动：三态门禁 + 价格优选候选人（确定性，非自动定标）
  const level = m.recommendation_level || (m.recommendation_blocked ? 'blocked' : 'conditional')
  const awardMode = m.award_mode || 'single_supplier'
  const allowSplit = awardMode === 'split_award'
  const pc = m.price_preferred_candidate || null
  // pc.supplier_id 兼容期内仍是准确的列身份（=col_id），与 s.id 的 join 不受影响。
  const pricePreferred = pc ? suppliers.find((s) => s.id === pc.supplier_id) : null
  // 拆单/最优组合总价：仅当招标文件允许分项授标才计算并展示
  const optimalTotal = allowSplit
    ? Math.round(rows.reduce((sum, row) => {
        const tots = row.suppliers.filter((c) => c.total !== null).map((c) => c.total as number)
        return sum + (tots.length ? Math.min(...tots) : 0)
      }, 0))
    : null
  const anomalyCount = rows.reduce(
    (n, r) => n + r.suppliers.filter((c) => c.alert_level === 'red').length, 0,
  )
  return {
    total_materials: rows.length,
    total_suppliers: suppliers.length,
    recommendation_level: level,
    award_mode: awardMode,
    allow_split: allowSplit,
    price_preferred_candidate: pricePreferred,
    price_preferred_total: pc ? pc.evaluated_total : null,
    committee_required: m.committee_required !== false,
    ranking: m.price_ranking || [],
    risks: m.risks || [],
    optimal_total: optimalTotal,
    anomaly_count: anomalyCount,
  }
})

// Anchor-matrix cell accounting (Req4 metrics reform)
const matrixCellStats = computed(() => {
  if (!matrixResult.value?.anchor_matrix) return null
  const rows = matrixResult.value.rows
  const n = matrixResult.value.suppliers.length
  let confirmed = 0, pending = 0, missing = 0
  for (const row of rows) {
    for (const cell of row.suppliers) {
      const st = cell.cell_status
      if (!st || st === 'quoted' || st === 'aggregated') {
        if (cell.price !== null) confirmed++
        else missing++
      } else if (st === 'pending') {
        pending++
      } else {
        missing++
      }
    }
  }
  const md = matrixResult.value.matrix_distribution
  return {
    anchors: rows.length,
    supplier_count: n,
    total_cells: rows.length * n,
    confirmed,
    pending,
    missing,
    quoted_ge_2: md?.quoted_ge_2_count ?? 0,
    quoted_full: md?.quoted_full_count ?? 0,
  }
})

// ─── AI Insight ─────────────────────────────────────────────────────────
const insightResult = ref<BidInsight | null>(null)
const insightLoading = ref(false)

async function fetchInsight() {
  if (!matrixResult.value || matrixResult.value.rows.length === 0) return
  // 三态都调用 AI：blocked 时让 AI 解释阻断原因（不推荐供应商），不再静默跳过
  insightLoading.value = true
  insightResult.value = null
  try {
    // 携带评标上下文（policy/排名/风险），AI 仅据此解释；行数截断控制体积
    const trimmed = {
      ...matrixResult.value,
      rows: matrixResult.value.rows.slice(0, 50),
    } as unknown as BidMatrixResult
    const { data } = await analysisApi.bidInsight(trimmed)
    insightResult.value = data
  } catch (e: any) {
    // AI insight is non-critical, but keep the real reason visible for testing.
    const detail = e?.response?.data?.detail || e?.response?.data?.error || e?.message || '分析请求失败'
    insightResult.value = { overall: '', recommendations: [], risks: [], error: detail }
  } finally {
    insightLoading.value = false
  }
}

// Auto-fetch insight when matrix result arrives
watch(matrixResult, (val) => {
  if (val && val.rows.length > 0) {
    fetchInsight()
  }
})

// Savings percentage — 仅当允许拆单（有 optimal_total）时才有意义
const savingsPercent = computed(() => {
  if (!matrixSummary.value || !matrixResult.value) return null
  const opt = matrixSummary.value.optimal_total
  if (opt == null) return null
  const totals = matrixResult.value.totals
  if (totals.length < 2) return null
  const avgTotal = totals.reduce((s, t) => s + t.total, 0) / totals.length
  if (avgTotal <= 0) return null
  const ratio = 1 - opt / avgTotal
  return ratio > 0 ? (ratio * 100).toFixed(1) : null
})

// ─── Tender List / Anchor matching ──────────────────────────────────────
const tenderMatchSummary = ref<AnchorMatchSummary | null>(null)


const tenderUploading = ref(false)
const anchorReviewResult = ref<AnchorReviewResult | null>(null)
const anchorReviewLoading = ref(false)

/** 从 axios 错误里取一条可读消息：detail 可能是字符串，也可能是结构化对象
 *  （如质量门 409 的 {error, message, failures}）。直接 message.error(对象) 会渲染成
 *  "[object Object]"，这里统一抽取 message/error 字段或 JSON 兜底。 */
function extractErrMsg(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof d === 'string') return d
  if (d && typeof d === 'object') {
    const o = d as { message?: unknown; error?: unknown }
    if (typeof o.message === 'string') return o.message
    if (typeof o.error === 'string') return o.error
    try { return JSON.stringify(d) } catch { return fallback }
  }
  return fallback
}

// Run tender list matching (called when entering Step 3)
async function runTenderMatch(): Promise<boolean> {
  if (!taskConfig.projectId) return false
  tenderUploading.value = true
  const form = new FormData()
  // Session is always confirmed before this point — load anchors from session, no file needed
  form.append('project_id', String(taskConfig.projectId))
  const cat = tenderCategory.value || taskConfig.category
  if (cat) form.append('category', cat)
  const sids = effectiveSupplierIds.value
  if (sids.length) form.append('supplier_ids', sids.join(','))
  const subIds = effectiveSubmissionIds.value
  if (subIds.length) form.append('submission_ids', subIds.join(','))
  try {
    const { data } = await analysisApi.tenderListMatch(form)
    tenderMatchSummary.value = data
    // 后端可能从 TLS session 推导了 category，回写到前端状态
    const resolvedCat = data.category || cat
    if (resolvedCat) {
      if (!tenderCategory.value) tenderCategory.value = resolvedCat
      if (!taskConfig.category) taskConfig.category = resolvedCat
    }
    return true
  } catch (e: unknown) {
    message.error(extractErrMsg(e, '招标清单匹配失败'))
    return false
  } finally {
    tenderUploading.value = false
  }
}

async function loadAnchorReview() {
  if (!taskConfig.projectId) return
  anchorReviewLoading.value = true
  try {
    const sids = effectiveSupplierIds.value
    const subIds = effectiveSubmissionIds.value
    const { data } = await analysisApi.anchorReview({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
      // submission identity is authoritative; supplier_ids kept only as fallback
      submission_ids: subIds.length ? subIds.join(',') : undefined,
      supplier_ids: sids.length ? sids.join(',') : undefined,
    })
    anchorReviewResult.value = data
  } catch (e: unknown) {
    message.error('加载复核数据失败')
  } finally {
    anchorReviewLoading.value = false
  }
}

async function runTenderMatchAndReview() {
  const sids = effectiveSupplierIds.value
  const subIds = effectiveSubmissionIds.value
  if (sids.length === 0 && subIds.length === 0) {
    message.error('请先完成供应商报价上传并「校对入库」，至少需要 1 家供应商的报价文件')
    return
  }
  matchRunning.value = true
  const ok = await runTenderMatch()
  if (!ok) {
    currentStep.value = 2
    matchRunning.value = false
    return
  }
  await loadAnchorReview()
  matchRunning.value = false
}

// All confirmed submission IDs (both known and unknown suppliers)
const effectiveSubmissionIds = computed((): number[] => {
  if (!useBatchMode.value) return []
  return batchFiles.value
    .filter(f => f.confirmed && f.confirmedSubmissionId != null)
    .map(f => f.confirmedSubmissionId!)
})

// Known-supplier IDs only (for backward compat with non-batch mode)
const effectiveSupplierIds = computed((): number[] => {
  if (useBatchMode.value) {
    return [...new Set(batchFiles.value.filter(f => f.confirmed && f.confirmedSupplierId).map(f => f.confirmedSupplierId!))]
  }
  return taskConfig.supplierIds
})

// Single-supplier mode: compare against history instead of across suppliers
const isSingleSupplierMode = computed(() => {
  const cols = useBatchMode.value ? effectiveSubmissionIds.value.length : effectiveSupplierIds.value.length
  return cols === 1
})

// ─── Data fetching ───────────────────────────────────────────────────────
async function fetchProjects() {
  try {
    const { data } = await projectApi.list({ page: 1, page_size: 100 })
    projects.value = data.items
  } catch {
    projects.value = []
  }
}
async function fetchSuppliers() {
  try {
    const { data } = await supplierApi.list({ page: 1, page_size: 100 })
    allSuppliers.value = data.items
  } catch {
    allSuppliers.value = []
  }
}

onMounted(async () => {
  await Promise.all([fetchProjects(), fetchSuppliers()])
  // 深链 / 刷新恢复：/compare/:projectId/:step?
  const pidParam = route.params.projectId
  const stepParam = route.params.step
  if (pidParam) {
    const pid = Number(pidParam)
    if (Number.isFinite(pid) && pid > 0) {
      taskConfig.projectId = pid   // 触发 watch(projectId) → 恢复 session + batchFiles
      if (stepParam !== undefined) {
        const st = Number(stepParam)
        if (Number.isFinite(st) && st >= 0 && st <= STEP_RESULTS) currentStep.value = st
      }
    }
  }
})

// URL 同步：项目/步骤变化时 replace 到 /compare/:projectId/:step（不新增历史栈）。
watch([currentStep, () => taskConfig.projectId], () => {
  const pid = taskConfig.projectId
  if (!pid) return
  const target = `/compare/${pid}/${currentStep.value}`
  if (route.path !== target) router.replace(target).catch(() => {})
})

// Initialise + clean up upload slots when supplier selection changes.
// AUDIT-FIX M1: previously we only ADDED entries — unchecking and re-checking
// a supplier kept the prior confirmed=true state, making bid-matrix include
// stale uploads.
// 切换品类时同步 session_id；多品类下用户手动切换即视为显式选择
watch(tenderCategory, (cat) => {
  if (cat && categorySessionMap.value[cat]) {
    tenderListSessionId.value = categorySessionMap.value[cat]
  }
  if (cat && confirmedCategories.value.length > 1) {
    categoryExplicitlySelected.value = true
  }
})

// 选择项目 / 刷新后恢复品类状态（404 = 新项目，静默处理）
watch(() => taskConfig.projectId, async (pid) => {
  confirmedCategories.value = []
  categorySessionMap.value = {}
  tenderCategory.value = ''
  taskConfig.category = ''
  tenderListSessionId.value = null
  categoryExplicitlySelected.value = false
  if (!pid) return
  try {
    const { data } = await analysisApi.tenderListCurrentSessions({ project_id: pid })
    const sessions = data.sessions ?? []
    confirmedCategories.value = sessions.map(s => s.category)
    categorySessionMap.value = Object.fromEntries(sessions.map(s => [s.category, s.id]))
    const primary = data.primary_category || ''
    tenderCategory.value = primary
    taskConfig.category = primary
    tenderListSessionId.value = categorySessionMap.value[primary] ?? null
    categoryExplicitlySelected.value = sessions.length === 1
  } catch {
    // 新项目无历史会话，保持重置状态
  }
  // 刷新可恢复：重建供应商报价卡片（batchFiles 为空时才恢复，避免覆盖会话内上传）
  await restoreBatchFiles(pid)
})

watch(() => taskConfig.supplierIds, (ids, prev) => {
  for (const sid of ids) {
    if (!supplierUploads[sid]) {
      supplierUploads[sid] = {
        job: null, items: [], confirmed: false, unknown_brands: [],
      }
    }
  }
  for (const sid of (prev ?? [])) {
    if (!ids.includes(sid)) {
      delete supplierUploads[sid]
    }
  }
}, { immediate: true })

// ─── Step navigation ─────────────────────────────────────────────────────
async function goNext() {
  if (currentStep.value === 0) {
    if (!canProceedFromConfig.value) {
      message.warning('请先选择项目')
      return
    }
    currentStep.value = 1
  } else if (currentStep.value === 1) {
    if (!pdfSupplement.value && !tenderPreview.value) {
      message.warning('请先上传招标文件 PDF')
      return
    }
    // 自动补填品类（优先顺序：PDF识别结果 > 第1步配置）
    if (!tenderCategory.value) {
      tenderCategory.value = pdfSupplement.value?.detected_category || taskConfig.category || ''
    }
    // PDF 主清单模式：items 内含 category，confirm 时会按 item 自身分组，无需 top-level category
    // Excel 模式：必须有品类才能归档
    const isPdfPrimary = !!pdfSupplement.value
    if (!isPdfPrimary && !tenderCategory.value) {
      message.warning('品类未识别，请重新上传招标文件')
      return
    }
    // excel_primary 模式下差异须人工确认；pdf_primary 模式只提示，不阻断
    if (
      pdfSupplement.value && reconcileResult.value
      && reconcileResult.value.recommended_source === 'excel'
      && !reconcileConfirmed.value
    ) {
      message.warning('招标文件 PDF 与 Excel 清单存在差异，请勾选「已确认差异」后继续')
      return
    }
    // 尚未锁定时，在跳步前完成锁定；已锁定则直接跳
    if (!tenderListSessionId.value) {
      await confirmTenderListVersion()
      if (!tenderListSessionId.value) return  // 锁定失败（含 unknown 未勾选），留在当前步
    }
    if (!taskConfig.category) taskConfig.category = tenderCategory.value
    currentStep.value = 2
  } else if (currentStep.value === 2) {
    if (!canProceedFromUpload.value) {
      message.warning('请为每家供应商点击「校对入库」')
      return
    }
    const hasEntries = useBatchMode.value
      ? effectiveSubmissionIds.value.length > 0
      : effectiveSupplierIds.value.length > 0
    if (!hasEntries) {
      message.error('未检测到已入库的供应商报价，请先完成「校对入库」')
      return
    }
    currentStep.value = 3
    runTenderMatchAndReview()
  } else if (currentStep.value === 3) {
    // v2.5: pending no longer blocks matrix generation — pending cells appear in matrix with orange status
    // Only warn (not block) so user can proceed to see the full anchor matrix
    if (!allPendingActioned.value) {
      const n = anchorReviewResult.value?.pending_items_total ?? anchorReviewResult.value?.low_conf_groups.length ?? 0
      message.warning(`仍有 ${n} 条待确认项，矩阵中以橙色"待确认"显示，不计入合计和推荐`)
    }
    currentStep.value = STEP_RESULTS
    runMatrix()
  }
}

function goBack() {
  if (currentStep.value > 0) {
    if (currentStep.value === STEP_RESULTS) {
      matrixResult.value = null
    }
    if (currentStep.value === 3) {
      anchorReviewResult.value = null
      tenderMatchSummary.value = null
    }
    currentStep.value -= 1
  }
}

// ─── Step 2: per-supplier upload handlers ────────────────────────────────
function onExtracted(supplierId: number, job: ExtractionJob) {
  // AUDIT-FIX M9: runtime guard instead of unchecked cast
  const items = asQuoteShape(job.result).items
  supplierUploads[supplierId] = {
    ...(supplierUploads[supplierId] || { items: [], confirmed: false, unknown_brands: [] }),
    job,
    items,
    confirmed: false,
  }
}

async function confirmSupplier(supplierId: number) {
  const slot = supplierUploads[supplierId]
  if (!slot || !slot.job) {
    message.warning('请先上传该供应商的报价单')
    return
  }
  const effectiveCategory = tenderCategory.value || taskConfig.category
  if (!effectiveCategory) {
    message.error('品类不能为空：请先完成招标清单识别后再入库')
    return
  }
  try {
    const { data } = await quoteApi.batchConfirm({
      job_id: slot.job.id,
      supplier_id: supplierId,
      project_id: taskConfig.projectId,
      category: effectiveCategory,
      overrides: slot.items as unknown as Array<Record<string, unknown>>,
      bid_status: taskConfig.bidStatus,
    })
    const result = data as BatchConfirmResult
    slot.confirmed = true
    slot.batch_id = result.batch_id
    message.success(`已入库 ${result.line_count} 条报价`)
  } catch (e) {
    message.error(extractErrMsg(e, '入库失败'))
  }
}

// Skip upload for a supplier (use existing historical data)
function skipSupplier(supplierId: number) {
  supplierUploads[supplierId] = {
    job: null,
    items: [],
    confirmed: true,
    unknown_brands: [],
  }
  message.info('已跳过该供应商上传，将使用历史数据')
}

// ─── Batch upload handlers ──────────────────────────────────────────────
// NOTE: Excel/CSV files now go through the same intakeApi.upload → ExtractionJob →
// batch-confirm pipeline as PDFs.  handleExcelBatchFile (legacy import_service fork)
// has been removed.  quoteApi.import / import_service are still used by the
// materials-library import screen; this compare flow no longer forks to them.

function handleBatchFile(file: File) {
  if (!file) return
  const duplicatePending = batchFiles.value.some(
    (entry) => entry.filename === file.name && !entry.confirmed,
  )
  if (duplicatePending) return

    const entry: BatchFileEntry = {
      id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      filename: file.name,
      status: 'uploading',
      stage: '准备上传',
      progressPct: 1,
      uploadPct: 1,
      jobId: null,
      detectedSupplierName: '',
      finalSupplierName: '',
      matchedSupplierId: null,
      items: [],
      confirmedSupplierId: null,
      confirmedSubmissionId: null,
      confirmed: false,
      error: '',
      pollTimer: null,
    }
    batchFiles.value.push(entry)
    const reactiveEntry = batchFiles.value[batchFiles.value.length - 1]
    uploadBatchFile(reactiveEntry, file)
}

async function uploadBatchFile(entry: BatchFileEntry, file: File) {
  const form = new FormData()
  form.append('file', file)
  form.append('type', 'quote')
  if (taskConfig.projectId) form.append('project_id', String(taskConfig.projectId))
  if (taskConfig.category) form.append('category', taskConfig.category)
  try {
    const { data } = await intakeApi.upload(form, {
      onUploadProgress: (evt) => {
        if (!evt.total) return
        const pct = Math.max(1, Math.min(99, Math.round((evt.loaded / evt.total) * 100)))
        entry.uploadPct = pct
        entry.progressPct = pct
        entry.stage = `上传中 ${pct}%`
      },
    })
    entry.jobId = data.id
    syncBatchProgress(entry, data)
    if (data.status === 'done') {
      onBatchJobDone(entry, data)
    } else if (data.status === 'failed') {
      entry.status = 'failed'
      entry.stage = '失败'
      entry.error = data.error || '识别失败'
    } else {
      entry.status = 'processing'
      startBatchPolling(entry)
    }
  } catch (e) {
    entry.status = 'failed'
    entry.stage = '失败'
    entry.error = (e as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail
      || (e as Error).message
      || '上传失败'
  }
}

function syncBatchProgress(entry: BatchFileEntry, job: ExtractionJob) {
  if (job.status === 'pending') {
    entry.status = 'processing'
    entry.stage = job.progress_stage || '排队中'
    entry.progressPct = job.progress_pct || 0
  } else if (job.status === 'running') {
    entry.status = 'processing'
    entry.stage = job.progress_stage || '识别中'
    entry.progressPct = job.progress_pct || 10
  } else if (job.status === 'done') {
    entry.status = 'done'
    entry.stage = '已识别'
    entry.progressPct = 100
  } else if (job.status === 'failed') {
    entry.status = 'failed'
    entry.stage = job.progress_stage || '失败'
  }
}

function currentBatchStepIndex(entry: BatchFileEntry) {
  const stage = entry.stage || ''
  if (entry.status === 'uploading') return 0
  if (entry.status === 'done' || entry.confirmed) return BATCH_PROGRESS_STEPS.length - 1
  if (stage.includes('已识别')) return 7
  if (stage.includes('整理')) return 6
  if (stage.includes('合并')) return 5
  if (stage.includes('识别') || stage.includes('完成第') || stage.includes('并发')) return 4
  if (stage.includes('拆分')) return 3
  if (stage.includes('渲染') || stage.includes('准备')) return 2
  if (stage.includes('接收') || stage.includes('排队')) return 1
  for (let i = BATCH_PROGRESS_STEPS.length - 1; i >= 0; i--) {
    if (entry.progressPct >= BATCH_PROGRESS_STEPS[i].pct) return i
  }
  return 0
}

function batchStepState(entry: BatchFileEntry, index: number) {
  const current = currentBatchStepIndex(entry)
  if (entry.status === 'failed' && index === current) return 'failed'
  if (index < current) return 'completed'
  if (index === current) return 'active'
  return 'pending'
}

function startBatchPolling(entry: BatchFileEntry) {
  if (entry.pollTimer) clearInterval(entry.pollTimer)
  let failures = 0
  entry.pollTimer = setInterval(async () => {
    if (!entry.jobId) return
    try {
      const { data } = await intakeApi.getJob(entry.jobId)
      failures = 0
      syncBatchProgress(entry, data)
      if (data.status === 'done') {
        if (entry.pollTimer) clearInterval(entry.pollTimer)
        entry.pollTimer = null
        onBatchJobDone(entry, data)
      } else if (data.status === 'failed') {
        if (entry.pollTimer) clearInterval(entry.pollTimer)
        entry.pollTimer = null
        entry.status = 'failed'
        entry.stage = '失败'
        entry.error = data.error || '识别失败'
      }
    } catch {
      failures++
      if (failures >= 5) {
        if (entry.pollTimer) clearInterval(entry.pollTimer)
        entry.pollTimer = null
        entry.status = 'failed'
        entry.stage = '失败'
        entry.error = '轮询超时'
      }
    }
  }, 2000)
}

function onBatchJobDone(entry: BatchFileEntry, job: ExtractionJob) {
  entry.status = 'done'
  entry.stage = '已识别'
  entry.progressPct = 100
  const shape = asQuoteShape(job.result)
  entry.items = shape.items
  entry.detectedSupplierName = shape.supplier_name || ''
  // Auto-match against known suppliers; initialize finalSupplierName from OCR
  if (entry.detectedSupplierName) {
    const name = entry.detectedSupplierName.replace(/\s/g, '').toLowerCase()
    const match = allSuppliers.value.find(
      (s) => s.name.replace(/\s/g, '').toLowerCase() === name
        || s.name.includes(entry.detectedSupplierName)
    )
    if (match) {
      entry.matchedSupplierId = match.id
      entry.finalSupplierName = match.name   // use canonical DB name when matched
    } else {
      entry.finalSupplierName = entry.detectedSupplierName
    }
  }
}

// ── 刷新可恢复：从后端重建 batchFiles（已入库 + 在途识别），续轮询/回填 items ──
async function restoreBatchFiles(pid: number) {
  if (batchFiles.value.length > 0) return   // 已有会话内卡片则不覆盖（仅刷新/深链时恢复）
  let data
  try {
    ({ data } = await analysisApi.compareState({ project_id: pid }))
  } catch {
    return   // 新项目无状态，静默
  }
  const restored: BatchFileEntry[] = []
  for (const s of data.submissions) {
    restored.push({
      id: `restored-sub-${s.submission_id}`,
      filename: s.filename || `已入库报价 #${s.submission_id}`,
      status: 'done', stage: `已入库 ${s.line_count} 条`,
      progressPct: 100, uploadPct: 100,
      jobId: s.job_id,
      detectedSupplierName: s.supplier_raw_name,
      finalSupplierName: s.supplier_raw_name,
      matchedSupplierId: s.supplier_id,
      items: [],
      confirmedSupplierId: s.supplier_id,
      confirmedSubmissionId: s.submission_id,
      confirmed: true, error: '', pollTimer: null,
    })
  }
  for (const j of data.inflight_jobs) {
    restored.push({
      id: `restored-job-${j.job_id}`,
      filename: j.filename || '报价文件',
      status: j.status === 'failed' ? 'failed' : 'processing',
      stage: j.progress_stage || (j.status === 'done' ? '已识别' : '识别中'),
      progressPct: j.progress_pct || 0, uploadPct: 100,
      jobId: j.job_id,
      detectedSupplierName: '', finalSupplierName: '', matchedSupplierId: null,
      items: [],
      confirmedSupplierId: null, confirmedSubmissionId: null,
      confirmed: false, error: '', pollTimer: null,
    })
  }
  batchFiles.value = restored
  // 在途任务：拉一次最新 job → running 续轮询；done 回填 items 供"校对入库"。
  for (const entry of batchFiles.value) {
    if (entry.confirmed || !entry.jobId || entry.status === 'failed') continue
    try {
      const { data: job } = await intakeApi.getJob(entry.jobId)
      syncBatchProgress(entry, job)
      if (job.status === 'done') onBatchJobDone(entry, job)
      else if (job.status === 'running' || job.status === 'pending') startBatchPolling(entry)
    } catch { /* ignore transient */ }
  }
}

async function confirmBatchEntry(entry: BatchFileEntry) {
  if (!entry.jobId) return

  // ── 品类校验 ──
  const categories = confirmedCategories.value
  const effectiveCategory =
    tenderCategory.value || taskConfig.category ||
    (categories.length === 1 ? categories[0] : '')
  if (!effectiveCategory) {
    message.error(categories.length > 1
      ? '采购清单包含多个品类，请先选择本报价所属品类'
      : '未恢复采购清单品类，请返回采购清单步骤重新确认')
    return
  }
  if (categories.length > 1 && !categoryExplicitlySelected.value) {
    message.error('采购清单包含多个品类，请先选择本报价所属品类')
    return
  }

  // 用 finalSupplierName（用户编辑后的名称）作为权威名称
  const supplierName = entry.finalSupplierName.trim() || entry.detectedSupplierName
  if (!supplierName) {
    message.warning('请输入供应商名称')
    return
  }

  // ── 三方冲突警告：文件名提示 / OCR 识别 / 当前输入名称不一致时要求确认 ─────
  const filenameHint = _extractSupplierHintFromFilename(entry.filename)
  const ocrName = entry.detectedSupplierName
  const conflicts: string[] = []
  if (filenameHint && !supplierName.includes(filenameHint) && !filenameHint.includes(supplierName.slice(0, 4))) {
    conflicts.push(`· 文件名提示：「${filenameHint}」`)
  }
  if (ocrName && ocrName !== supplierName && !ocrName.includes(supplierName) && !supplierName.includes(ocrName.slice(0, 4))) {
    conflicts.push(`· OCR 识别：「${ocrName}」`)
  }
  if (conflicts.length > 0) {
    const ok = window.confirm(
      `供应商名称存在冲突，请确认：\n${conflicts.join('\n')}\n· 当前输入：「${supplierName}」\n\n确认以「${supplierName}」入库？`
    )
    if (!ok) return
  }

  try {
    // supplier_id 由用户主动选择（matchedSupplierId）决定；编辑名称后若不再匹配则为 null（陌生供应商）
    const supplierId = entry.matchedSupplierId ?? undefined
    const { data } = await quoteApi.batchConfirm({
      job_id: entry.jobId,
      supplier_id: supplierId,
      supplier_name: supplierName,
      project_id: taskConfig.projectId,
      category: effectiveCategory,
      overrides: entry.items as unknown as Array<Record<string, unknown>>,
      bid_status: taskConfig.bidStatus,
    })
    entry.confirmed = true
    entry.confirmedSupplierId = data.supplier_id ?? null
    entry.confirmedSubmissionId = data.submission_id ?? null
    const unknownNote = supplierId ? '' : '（陌生供应商，仅用于本次比价）'
    message.success(`${supplierName}${unknownNote}：已入库 ${data.line_count} 条报价`)
  } catch (e: unknown) {
    const resp = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (resp && typeof resp === 'object' && (resp as Record<string, unknown>).error === 'supplier_alias_conflict') {
      const d = resp as { message: string; candidates: { id: number; name: string; similarity: number }[] }
      const topMatch = d.candidates[0]
      // 自动选最相似的候选，提示用户确认
      const confirmed = window.confirm(
        `${d.message}\n\n最相似：「${topMatch.name}」(相似度 ${Math.round(topMatch.similarity * 100)}%)\n\n点「确定」合并到该供应商，点「取消」手动选择`
      )
      if (confirmed) {
        entry.matchedSupplierId = topMatch.id
        await confirmBatchEntry(entry)  // 用确认的 supplier_id 重试
      } else {
        message.warning(`请在「供应商」下拉里手动选择正确的供应商后再入库`)
      }
    } else {
      message.error(extractErrMsg(e, '入库失败'))
    }
  }
}

async function removeBatchEntry(entry: BatchFileEntry) {
  if (entry.pollTimer) clearInterval(entry.pollTimer)
  // 已入库 → supersede submission；在途/失败（有 jobId 无 submission）→ 标记 job removed；
  // 二者皆持久化到后端，避免刷新后 compare-state 把卡片再拉回来。
  try {
    if (entry.confirmed && entry.confirmedSubmissionId != null) {
      await quoteApi.supersedeSubmission(entry.confirmedSubmissionId)
      message.success('已移除该报价')
    } else if (entry.jobId) {
      await quoteApi.removeJob(entry.jobId)
    }
  } catch (e: unknown) {
    message.error(extractErrMsg(e, '移除失败'))
    return
  }
  batchFiles.value = batchFiles.value.filter((f) => f.id !== entry.id)
}

// 一键移除：清空当前项目下全部供应商报价。
// - 已入库：项目级 supersede（可复活）。
// - 在途失败/已识别待确认（非运行中）：逐个标记 job removed。
// - 运行中（识别未完）：保留，不强制中断后台线程。
async function removeAllBatchEntries() {
  const pid = taskConfig.projectId
  if (pid) {
    try {
      const { data } = await quoteApi.supersedeProjectSubmissions(pid)
      message.success(`已移除全部已入库报价（${data.count} 条）`)
    } catch (e: unknown) {
      message.error(extractErrMsg(e, '一键移除失败'))
      return
    }
  }
  // 非运行中的在途任务（失败/已识别待确认）→ 标记 removed
  const inflightDone = batchFiles.value.filter(
    (f) => !f.confirmed && f.jobId && (f.status === 'failed' || f.status === 'done'),
  )
  for (const f of inflightDone) {
    try { await quoteApi.removeJob(f.jobId!) } catch { /* best-effort */ }
  }
  // 清理 UI：移除已入库 + 已处理在途；保留运行中的卡片（及其轮询）
  for (const f of batchFiles.value) {
    const keep = !f.confirmed && (f.status === 'uploading' || f.status === 'processing')
    if (!keep && f.pollTimer) clearInterval(f.pollTimer)
  }
  batchFiles.value = batchFiles.value.filter(
    (f) => !f.confirmed && (f.status === 'uploading' || f.status === 'processing'),
  )
}

// 从文件名中提取供应商名称提示（用于冲突检测）
// 例：「泰科龙投标文件.pdf」→「泰科龙」；「上海绵存报价单.xlsx」→「上海绵存」
function _extractSupplierHintFromFilename(filename: string): string {
  const base = filename.replace(/\.(pdf|xlsx?|csv|docx?)$/i, '')
  // 按常见切割词分割，取第一个非空段
  const parts = base.split(/[投标报价文件单_\-\s··【】()（）]+/)
  return (parts[0] || '').trim()
}

onBeforeUnmount(() => {
  for (const f of batchFiles.value) {
    if (f.pollTimer) clearInterval(f.pollTimer)
  }
  stopTenderPoll()
})

// ─── Step 4: run bid-matrix ──────────────────────────────────────────────
async function runMatrix() {
  const sids = useBatchMode.value ? effectiveSupplierIds.value : taskConfig.supplierIds
  const subIds = effectiveSubmissionIds.value  // all confirmed submissions (batch mode only)
  if (useBatchMode.value ? subIds.length < 1 : sids.length < 1) {
    message.warning('至少需要 1 家供应商的报价才能比价')
    return
  }
  analyzing.value = true
  matrixResult.value = null
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: taskConfig.projectId,
      supplier_ids: sids,
      submission_ids: subIds.length ? subIds : undefined,
      category: tenderCategory.value || taskConfig.category || undefined,
    })
    matrixResult.value = data
    if ((data.rows ?? []).length === 0) {
      message.warning('当前条件下未找到可比的报价数据')
    }
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '比价分析失败'
    message.error(detail)
  } finally {
    analyzing.value = false
  }
}
</script>

<template>
  <div class="compare-page">
    <!-- Page header -->
    <div class="compare-page__header">
      <div>
        <h1 class="compare-page__title">招标比价分析</h1>
        <div class="compare-page__subtitle">
          按"配置→录入报价→比价结果"分步完成；支持 PDF/扫描件自动识别 + Excel 直接导入
        </div>
      </div>
    </div>

    <!-- Steps indicator -->
    <a-steps :current="currentStep" style="margin-bottom:20px">
      <a-step title="配置任务" description="选项目 + 供应商" />
      <a-step title="采购清单" description="招标 PDF 主清单 + Excel 对照参考" />
      <a-step title="供应商报价" description="PDF / Excel 批量上传" />
      <a-step title="对齐核查" description="确认低置信匹配项" />
      <a-step title="比价矩阵" :description="isSingleSupplierMode ? '报价 vs 历史价格' : '横向对比 + 推荐'" />
    </a-steps>

    <!-- Step 0: Configure -->
    <a-card v-if="currentStep === 0" :body-style="{ padding: '20px' }">
      <a-form layout="vertical">
        <a-form-item label="项目（必选）" required>
          <a-select
            v-model:value="taskConfig.projectId"
            placeholder="选择项目"
            allow-clear
            show-search
            :filter-option="(input: string, opt: { label?: unknown }) => String(opt.label ?? '').includes(input)"
          >
            <a-select-option
              v-for="p in projects"
              :key="p.id"
              :value="p.id"
              :label="p.name"
            >
              {{ p.name }}
              <span v-if="p.code" style="color:rgba(0,0,0,0.45);margin-left:6px">{{ p.code }}</span>
            </a-select-option>
            <template #dropdownRender="{ menuNode }">
              <component :is="menuNode" />
              <a-divider style="margin:4px 0" />
              <div style="padding:4px 8px;cursor:pointer;display:flex;align-items:center;gap:4px;color:#1677ff" @mousedown.prevent @click="openNewProjectModal">
                <PlusOutlined /> 新建项目
              </div>
            </template>
          </a-select>
        </a-form-item>

        <a-form-item>
          <a-checkbox
            :checked="taskConfig.bidStatus === '未中标'"
            @change="(e: any) => taskConfig.bidStatus = e.target.checked ? '未中标' : ''"
          >
            标记为未中标清单
          </a-checkbox>
          <div style="margin-top:4px;font-size:12px;color:rgba(0,0,0,0.45)">
            勾选后导入的报价不在热力图/气泡图中显示，但参与比价与邀标推荐
          </div>
        </a-form-item>

        <a-form-item label="参与供应商（可选）">
          <a-select
            v-model:value="taskConfig.supplierIds"
            mode="multiple"
            placeholder="预选供应商，或留空 → 下一步批量上传自动识别"
            show-search
            :filter-option="(input: string, opt: { label?: unknown }) => String(opt.label ?? '').includes(input)"
            style="width:100%"
          >
            <a-select-option
              v-for="s in allSuppliers"
              :key="s.id"
              :value="s.id"
              :label="s.name"
            >
              {{ s.name }}
            </a-select-option>
          </a-select>
          <div style="margin-top:6px;font-size:12px;color:rgba(0,0,0,0.45)">
            {{ taskConfig.supplierIds.length > 0
              ? `已选 ${taskConfig.supplierIds.length} 家`
              : '不选也行 — 下一步上传报价 PDF 后系统自动识别供应商'
            }}
          </div>
        </a-form-item>
      </a-form>
    </a-card>

    <!-- Step 2: 采购清单 (PDF主清单 + Excel对照参考) -->
    <a-card v-else-if="currentStep === 1" :body-style="{ padding: '20px' }">

      <!-- ── A: 招标文件 PDF（主清单）──────────────────────────────── -->
      <div style="margin-bottom:16px">
        <h3 style="margin:0 0 3px;font-size:15px;font-weight:600">
          <FilePdfOutlined style="color:#cf1322;margin-right:6px" />招标文件 PDF（主清单）
        </h3>
        <div style="font-size:12px;color:rgba(0,0,0,0.45)">上传招标文件 PDF，系统自动识别投标清单页与品牌要求表，作为比价基础清单</div>
      </div>

      <!-- A1: 未上传 -->
      <div v-if="!pdfSupplement && !tenderPreviewing">
        <a-upload-dragger
          accept=".pdf"
          :show-upload-list="false"
          :before-upload="(f: File) => { uploadTenderPdf(f); return false; }"
          style="margin-bottom:12px"
        >
          <p class="ant-upload-drag-icon"><FilePdfOutlined style="color:#cf1322;font-size:36px" /></p>
          <p class="ant-upload-text">点击或拖入招标文件 PDF</p>
          <p class="ant-upload-hint">自动识别投标清单页与品牌要求表，通常 30~90 秒完成</p>
        </a-upload-dragger>
        <a-alert v-if="tenderJobError" type="error" show-icon :message="tenderJobError" />
      </div>

      <!-- A2: 识别中（仅 PDF 尚未识别完成时显示） -->
      <div v-else-if="!pdfSupplement && tenderPreviewing"
        style="padding:16px;border:1px dashed #d9d9d9;border-radius:6px">
        <div style="margin-bottom:10px;font-size:13px;color:#555">
          <LoadingOutlined spin style="margin-right:6px;color:#1677ff" />{{ tenderJobStage || '识别中...' }}（通常 30~90 秒）
        </div>
        <a-progress :percent="tenderJobPct" :status="tenderJobError ? 'exception' : 'active'" />
        <a-alert v-if="tenderJobError" type="error" show-icon style="margin-top:8px" :message="tenderJobError" />
      </div>

      <!-- A3: 已识别 -->
      <template v-else-if="pdfSupplement">

        <!-- 结论卡片 -->
        <div :style="{
          background: pdfConclusionStatus === 'ok' ? '#f6ffed' : pdfConclusionStatus === 'warning' ? '#fffbe6' : '#fff2f0',
          border: `1px solid ${pdfConclusionStatus === 'ok' ? '#b7eb8f' : pdfConclusionStatus === 'warning' ? '#ffe58f' : '#ffa39e'}`,
          borderRadius: '8px', padding: '14px 16px', marginBottom: '16px',
          display: 'flex', alignItems: 'flex-start', gap: '12px'
        }">
          <CheckCircleOutlined v-if="pdfConclusionStatus === 'ok'" style="color:#52c41a;font-size:22px;flex-shrink:0;margin-top:2px" />
          <WarningOutlined v-else-if="pdfConclusionStatus === 'warning'" style="color:#fa8c16;font-size:22px;flex-shrink:0;margin-top:2px" />
          <CloseCircleOutlined v-else style="color:#ff4d4f;font-size:22px;flex-shrink:0;margin-top:2px" />
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:15px;color:#1a1a1a">{{ pdfConclusionTitle }}</div>
            <div style="font-size:13px;color:rgba(0,0,0,0.55);margin-top:3px">{{ pdfConclusionDesc }}</div>
            <div v-if="pdfDiffSummary" style="font-size:12px;color:#d46b08;margin-top:4px">{{ pdfDiffSummary }}</div>
            <div v-if="reconcileLoading" style="font-size:12px;color:#666;margin-top:4px">
              <LoadingOutlined spin style="margin-right:4px" />正在与Excel对账...
            </div>
          </div>
          <a-button size="small" type="text" style="flex-shrink:0"
            @click="pdfSupplement = null; tenderBrandRequirement = []; tenderSupplierBrands = []; reconcileResult = null; reconcileConfirmed = false; tenderJobError = ''; showPageOverride = false; showQualityDetails = false; excelOnlyItemActions = {}">
            重新上传
          </a-button>
        </div>

        <!-- 招标品牌要求 -->
        <div v-if="tenderBrandRequirement.length || tenderSupplierBrands.length"
          style="margin-bottom:14px;padding:14px 16px;border:1px solid #bae0ff;background:#e6f4ff;border-radius:8px">
          <div style="font-size:13px;font-weight:600;color:#0958d9;margin-bottom:12px">
            招标品牌要求
            <span style="font-weight:400;color:rgba(0,0,0,0.35);font-size:12px;margin-left:6px">第 {{ tenderDetectedPages?.brand ?? '?' }} 页</span>
          </div>

          <div v-if="tenderBrandRequirement.length" style="margin-bottom:12px">
            <div style="font-size:12px;color:rgba(0,0,0,0.55);margin-bottom:6px">业主指定品牌</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <div v-for="b in tenderBrandRequirement" :key="b.brand_en"
                style="padding:5px 14px;background:#fff;border:1px solid #91caff;border-radius:6px;font-size:13px;font-weight:600;color:#003eb3">
                {{ b.brand_en }}&nbsp;<span style="font-weight:400;color:#555">{{ b.brand_cn }}</span>
              </div>
            </div>
          </div>

          <div v-if="tenderSupplierBrands.length">
            <div style="font-size:12px;color:rgba(0,0,0,0.55);margin-bottom:8px">各投标方参与品牌</div>
            <div style="display:flex;flex-direction:column;gap:8px">
              <div v-for="(s, i) in tenderSupplierBrands" :key="i"
                style="display:flex;align-items:center;gap:8px;font-size:13px">
                <span style="color:#1a1a1a;min-width:88px;font-weight:500">{{ s.supplier_name }}</span>
                <RightOutlined style="color:rgba(0,0,0,0.25);font-size:10px;flex-shrink:0" />
                <span style="background:#e6fffb;border:1px solid #87e8de;padding:3px 12px;border-radius:5px;color:#00695c;font-weight:600">{{ s.brand }}</span>
                <a-tooltip v-if="s.supplier_id == null"
                  title="系统未找到与此供应商名称精确匹配的记录，比价时将按名称模糊对应，请确认供应商名称无误">
                  <span style="display:inline-flex;align-items:center;gap:3px;cursor:help;padding:2px 6px;background:#fff7e6;border-radius:4px;border:1px solid #ffe58f">
                    <WarningOutlined style="color:#fa8c16;font-size:12px" />
                    <span style="font-size:12px;color:#d46b08">供应商待确认</span>
                  </span>
                </a-tooltip>
              </div>
            </div>
          </div>
        </div>

        <!-- 质量摘要 -->
        <div v-if="pdfQm" style="margin-bottom:10px;padding:10px 14px;background:#fafafa;border-radius:6px">
          <div style="display:flex;flex-wrap:wrap;gap:6px 20px;font-size:12px;color:rgba(0,0,0,0.65)">
            <span>采购项：<strong style="color:#1a1a1a">{{ pdfSupplement!.row_count }}</strong></span>
            <span>材质覆盖：<strong :style="{ color: pdfQm.material_columns_filled_rate < 0.4 ? '#cf1322' : '#389e0d' }">{{ pct(pdfQm.material_columns_filled_rate) }}</strong></span>
            <span>来源追踪：<strong :style="{ color: pdfQm.source_ref_coverage < 1.0 ? '#d46b08' : '#389e0d' }">{{ pct(pdfQm.source_ref_coverage) }}</strong></span>
            <span>数量解析：<strong :style="{ color: pdfQm.qty_parse_success_rate < 0.8 ? '#d46b08' : '#389e0d' }">{{ pct(pdfQm.qty_parse_success_rate) }}</strong></span>
            <span v-if="pdfQm.table_grid_pages.length || pdfQm.html_fallback_pages.length" style="color:rgba(0,0,0,0.45)">
              识别路径：<template v-if="pdfQm.table_grid_pages.length">{{ pdfQm.table_grid_pages.length }}页标准解析</template><template v-if="pdfQm.html_fallback_pages.length">{{ pdfQm.table_grid_pages.length ? ' · ' : '' }}{{ pdfQm.html_fallback_pages.length }}页OCR增强解析</template>
            </span>
          </div>
          <div v-if="pdfQm.seq_missing.length || pdfQm.seq_duplicate.length" style="margin-top:6px;font-size:12px;color:#d46b08">
            <span v-if="pdfQm.seq_missing.length">序号缺失 {{ pdfQm.seq_missing.length }} 项（{{ pdfQm.seq_missing.slice(0, 5).join(', ') }}{{ pdfQm.seq_missing.length > 5 ? '…' : '' }}）</span>
            <span v-if="pdfQm.seq_duplicate.length" style="margin-left:10px">序号重复：{{ pdfQm.seq_duplicate.join(', ') }}</span>
          </div>
          <a-button type="link" size="small"
            style="padding:0;height:auto;margin-top:6px;font-size:12px;color:rgba(0,0,0,0.35)"
            @click="showQualityDetails = !showQualityDetails">
            {{ showQualityDetails ? '收起识别详情 ↑' : '识别详情 ↓' }}
          </a-button>
          <div v-if="showQualityDetails && pdfDiagnostics.length" style="margin-top:8px">
            <div style="display:flex;flex-wrap:wrap;gap:4px">
              <a-tooltip v-for="d in pdfDiagnostics" :key="d.page"
                :title="`第${d.page}页 · ${d.input_mode === 'table_grid' ? '标准解析' : 'OCR增强解析'}${d.fallback_reason ? '（原因：' + d.fallback_reason + '）' : ''} · 预计${d.expected_rows}行 · 提取${d.extracted_rows}行${d.thinking_retry ? ' · 已重试' : ''}`">
                <a-tag :color="d.input_mode === 'table_grid' ? 'green' : 'orange'"
                  style="cursor:default;font-size:11px;padding:0 6px;line-height:20px">
                  第{{ d.page }}页<template v-if="d.fallback_reason"> ⚠</template>
                </a-tag>
              </a-tooltip>
            </div>
            <div style="margin-top:4px;font-size:11px;color:rgba(0,0,0,0.35)">绿色=标准解析 · 橙色=OCR增强解析（hover查看详情）</div>
          </div>
        </div>

        <!-- 页码修正（折叠） -->
        <div style="margin-bottom:6px">
          <a-button type="link" size="small"
            style="padding:0;height:auto;font-size:12px;color:rgba(0,0,0,0.35)"
            @click="showPageOverride = !showPageOverride">
            页码识别有误？点击修正
          </a-button>
          <div v-if="showPageOverride"
            style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:12px">
            <span style="color:#555">清单页</span>
            <a-input v-model:value="overrideBidlistPages" size="small" style="width:100px" placeholder="如 14-18" />
            <span style="color:#555">品牌页</span>
            <a-input v-model:value="overrideBrandPage" size="small" style="width:60px" placeholder="如 13" />
            <a-button size="small" :loading="tenderPreviewing" @click="reExtractTenderPdf">按指定页重新识别</a-button>
          </div>
        </div>

      </template>

      <!-- ── B: Excel参考差异（PDF+Excel均已加载且有独有项时显示）───── -->
      <template v-if="pdfSupplement && tenderPreview && excelOnlyItems.length > 0">
        <a-divider style="margin:16px 0 12px" />
        <div style="padding:14px 16px;background:#fffbe6;border:1px solid #ffe58f;border-radius:8px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:6px">
            <div style="font-size:13px;font-weight:600;color:#874d00">
              <WarningOutlined style="margin-right:6px" />采购清单Excel参考差异（{{ excelOnlyItems.length }} 项）
            </div>
            <div style="font-size:12px;color:rgba(0,0,0,0.45)">以下项仅在Excel中，不在PDF主清单内，请逐项处理</div>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">
            <div v-for="item in excelOnlyItems" :key="String(item.seq)"
              :style="{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '8px 10px', borderRadius: '6px', fontSize: '12px',
                background: excelOnlyItemActions[String(item.seq)] === 'add' ? '#f6ffed'
                          : excelOnlyItemActions[String(item.seq)] === 'ignore' ? '#f5f5f5' : '#fff',
                border: `1px solid ${excelOnlyItemActions[String(item.seq)] === 'add' ? '#b7eb8f'
                        : excelOnlyItemActions[String(item.seq)] === 'ignore' ? '#e0e0e0' : '#ffd666'}`,
                opacity: excelOnlyItemActions[String(item.seq)] === 'ignore' ? '0.55' : '1',
              }"
            >
              <span style="color:rgba(0,0,0,0.4);flex-shrink:0;width:32px;text-align:center">{{ item.seq }}</span>
              <span style="flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
                :style="{ textDecoration: excelOnlyItemActions[String(item.seq)] === 'ignore' ? 'line-through' : 'none' }">
                {{ item.name || '—' }}
              </span>
              <span style="color:rgba(0,0,0,0.5);width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0">{{ item.spec || '—' }}</span>
              <span style="color:rgba(0,0,0,0.5);width:36px;text-align:center;flex-shrink:0">{{ item.unit || '—' }}</span>
              <span style="color:rgba(0,0,0,0.5);width:40px;text-align:right;flex-shrink:0">{{ item.qty ?? '—' }}</span>
              <span style="color:rgba(0,0,0,0.35);flex-shrink:0;font-size:11px;width:80px;text-align:right">Excel参考独有</span>
              <div style="flex-shrink:0;display:flex;gap:2px" v-if="!excelOnlyItemActions[String(item.seq)]">
                <a-button size="small" type="text" style="color:rgba(0,0,0,0.4);font-size:11px"
                  @click="setExcelItemAction(String(item.seq), 'ignore')">忽略</a-button>
                <a-button size="small" type="link" style="padding:0 4px;font-size:11px"
                  @click="setExcelItemAction(String(item.seq), 'add')">加入主清单</a-button>
                <a-button size="small" type="text" style="color:#d46b08;font-size:11px"
                  @click="setExcelItemAction(String(item.seq), 'pending')">标记待确认</a-button>
              </div>
              <div style="flex-shrink:0;display:flex;align-items:center;gap:4px" v-else>
                <a-tag v-if="excelOnlyItemActions[String(item.seq)] === 'add'" color="success" style="margin:0">已加入主清单</a-tag>
                <a-tag v-else-if="excelOnlyItemActions[String(item.seq)] === 'ignore'" color="default" style="margin:0">已忽略</a-tag>
                <a-tag v-else-if="excelOnlyItemActions[String(item.seq)] === 'pending'" color="warning" style="margin:0">待确认</a-tag>
                <a-button size="small" type="text" style="color:rgba(0,0,0,0.3);font-size:11px;padding:0 4px"
                  @click="clearExcelItemAction(String(item.seq))">撤销</a-button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- ── C: 采购清单 Excel（对照参考，可选）──────────────────── -->
      <a-divider style="margin:16px 0 12px" />
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600;color:#555">
          <FileExcelOutlined style="color:#52c41a;margin-right:6px" />采购清单 Excel（对照参考）
        </div>
        <a-button size="small" type="link" @click="showExcelPanel = !showExcelPanel">
          {{ showExcelPanel ? '收起' : '展开' }}
        </a-button>
      </div>
      <div v-if="showExcelPanel">
        <div v-if="!tenderPreview">
          <a-upload-dragger
            accept=".xlsx,.xls"
            :show-upload-list="false"
            :disabled="tenderPreviewing"
            :before-upload="(f: File) => { previewTenderList(f); return false; }"
            style="margin-bottom:10px"
          >
            <p class="ant-upload-drag-icon"><InboxOutlined style="color:#52c41a;font-size:30px" /></p>
            <p class="ant-upload-text" style="font-size:13px">点击或拖入采购清单 Excel</p>
            <p class="ant-upload-hint">支持 .xlsx / .xls · 上传后自动与 PDF 主清单对账</p>
          </a-upload-dragger>
          <div v-if="tenderPreviewing" style="padding:8px 0;text-align:center;color:#666;font-size:12px">
            <LoadingOutlined spin style="margin-right:6px" />解析中...
          </div>
        </div>
        <div v-else>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;padding:10px 12px;background:#f6f8fa;border-radius:6px">
            <FileExcelOutlined style="color:#52c41a;font-size:18px;flex-shrink:0" />
            <div style="flex:1">
              <div style="font-weight:600;font-size:14px">{{ tenderFile?.name }}</div>
              <div style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:2px">
                参考清单 · {{ tenderPreview.total }} 条<template v-if="tenderCategory"> · {{ tenderCategory }}</template>
              </div>
            </div>
            <a-button size="small" type="link"
              @click="tenderPreview = null; tenderFile = null; reconcileResult = null; reconcileConfirmed = false; excelOnlyItemActions = {}">
              重新上传
            </a-button>
          </div>
          <a-table
            :data-source="tenderPreview.items"
            :row-key="(r: Record<string,unknown>) => String(r.seq)"
            size="small" :pagination="{ pageSize: 8, size: 'small' }" :scroll="{ x: 700 }"
            :columns="[
              { title: '序号', dataIndex: 'seq', width: 56 },
              { title: '品名', dataIndex: 'name', ellipsis: true },
              { title: '规格', dataIndex: 'spec', width: 110, ellipsis: true },
              { title: '专业', dataIndex: 'profession', key: 'profession', width: 70 },
              { title: '单位', dataIndex: 'unit', width: 52 },
              { title: '数量', dataIndex: 'qty', width: 64, customRender: ({ text }: { text: number | null }) => text ?? '—' },
            ]"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'profession'">
                <span style="color:rgba(0,0,0,0.45);font-size:12px">{{ record.profession || '—' }}</span>
              </template>
            </template>
          </a-table>
          <div v-if="tenderPreview.unknown_count > 0"
            style="margin-top:10px;padding:8px 12px;background:#fff7e6;border:1px solid #ffa940;border-radius:4px;display:flex;align-items:center;gap:8px;font-size:13px">
            <a-checkbox v-model:checked="forceUnknownCategory">
              强制归入默认品类「{{ tenderCategory || taskConfig.category }}」（{{ tenderPreview.unknown_count }} 项未识别将写入审计标记）
            </a-checkbox>
          </div>
          <!-- 字段差异摘要（reconcile 已完成时显示） -->
          <div v-if="reconcileResult && reconcileResult.field_mismatches.length" style="margin-top:12px">
            <div style="font-size:12px;color:rgba(0,0,0,0.55);margin-bottom:6px">
              字段差异（{{ reconcileResult.field_mismatches.length }} 处）—— 以PDF主清单值为准
            </div>
            <a-table
              :data-source="reconcileResult.field_mismatches"
              :row-key="(_r: Record<string,unknown>, i: number) => i"
              size="small" :pagination="{ pageSize: 5, size: 'small' }"
              :columns="[
                { title: '序号', dataIndex: 'seq', width: 56 },
                { title: '字段', dataIndex: 'field', width: 60 },
                { title: 'Excel参考值', dataIndex: 'xlsx_value', ellipsis: true },
                { title: 'PDF主清单值', dataIndex: 'pdf_value', ellipsis: true },
              ]"
            />
          </div>
        </div>
      </div>
    </a-card>

    <!-- Step 2: Upload Supplier Quotes -->
    <a-card v-else-if="currentStep === 2" :body-style="{ padding: '20px' }">
      <!-- Context bar -->
      <div style="margin-bottom:16px;padding:10px 12px;background:#f6f8fa;border-radius:6px;display:flex;gap:10px;align-items:center;font-size:12px;color:rgba(0,0,0,0.55);flex-wrap:wrap">
        <template v-if="pdfSupplement">
          <FilePdfOutlined style="color:#cf1322" />
          <span>招标文件PDF（主清单）：<strong style="color:#1a1a1a">{{ tenderPdfFile?.name }}</strong>（{{ pdfSupplement.row_count }} 项）</span>
        </template>
        <template v-else>
          <FileExcelOutlined style="color:#52c41a" />
          <span>基础清单（Excel）：<strong style="color:#1a1a1a">{{ tenderFile?.name }}</strong>（{{ tenderPreview?.total ?? 0 }} 项）</span>
        </template>
        <span v-if="tenderCategory">· 品类：<strong>{{ tenderCategory }}</strong></span>
        <template v-if="tenderBrandRequirement.length">
          <span>· 品牌要求：</span>
          <a-tag v-for="b in tenderBrandRequirement" :key="b.brand_en" color="blue" style="margin:0 2px">{{ b.brand_cn }}</a-tag>
        </template>
      </div>

      <!-- Batch mode: no suppliers pre-selected -->
      <template v-if="useBatchMode">
        <a-upload-dragger
          :multiple="true"
          accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls,.csv"
          :show-upload-list="false"
          :before-upload="(file: File) => { handleBatchFile(file); return false; }"
        >
          <p class="ant-upload-drag-icon"><CloudUploadOutlined /></p>
          <p class="ant-upload-text">拖入所有供应商的报价文件</p>
          <p class="ant-upload-hint">支持 PDF / 图片（OCR 识别）、Excel / CSV（直接解析）· 多文件同时上传</p>
        </a-upload-dragger>

        <div v-if="batchProgress && batchProgress.total > 1" class="batch-overall">
          <span>
            识别进度：{{ batchProgress.done }}/{{ batchProgress.total }} 完成
            <template v-if="batchProgress.failed > 0">
              · <span style="color:#ff4d4f">{{ batchProgress.failed }} 失败</span>
            </template>
            <template v-if="batchProgress.processing > 0">
              · {{ batchProgress.processing }} 处理中
            </template>
          </span>
          <a-progress
            :percent="Math.round((batchProgress.done / batchProgress.total) * 100)"
            :status="batchProgress.failed > 0 ? 'exception' : batchProgress.done === batchProgress.total ? 'success' : 'active'"
            size="small"
            style="width:200px;margin-left:12px"
          />
        </div>
        <div v-if="batchFiles.length > 0" class="batch-list-header">
          <span class="batch-list-header__count">共 {{ batchFiles.length }} 份报价</span>
          <a-popconfirm
            title="一键移除将清空当前项目下全部已上传/已入库的供应商报价（标记 superseded，重新上传可恢复）。确认移除全部？"
            ok-text="移除全部"
            cancel-text="取消"
            @confirm="removeAllBatchEntries"
          >
            <a-button size="small" danger>一键移除</a-button>
          </a-popconfirm>
        </div>
        <div v-if="batchFiles.length > 0" class="batch-list">
          <div v-for="f in batchFiles" :key="f.id" class="batch-card" :class="{ 'batch-card--done': f.confirmed }">
            <div class="batch-card__head">
              <LoadingOutlined v-if="f.status === 'uploading' || f.status === 'processing'" spin style="color:#1890ff" />
              <CheckOutlined v-else-if="f.confirmed" style="color:#52c41a" />
              <CloseCircleOutlined v-else-if="f.status === 'failed'" style="color:#ff4d4f" />
              <CheckCircleOutlined v-else style="color:#1890ff" />
              <span class="batch-card__filename">{{ f.filename }}</span>
              <a-tag v-if="f.status === 'uploading'" color="blue">
                {{ f.stage }} · {{ f.progressPct }}%
              </a-tag>
              <a-tag v-else-if="f.status === 'processing'" color="blue">
                {{ f.stage }} · {{ f.progressPct }}%
              </a-tag>
              <a-tag v-else-if="f.status === 'failed'" color="red">失败</a-tag>
              <a-tag v-else-if="f.confirmed && !f.jobId" color="green">Excel 已导入</a-tag>
              <a-tag v-else-if="f.confirmed" color="green">已入库</a-tag>
              <a-tag v-else color="cyan">{{ f.stage }} · {{ f.items.length }} 项</a-tag>
              <a-popconfirm
                v-if="f.confirmed"
                title="移除后该报价将从本次比价中删除（标记 superseded，重新上传可恢复）。确认移除？"
                ok-text="移除"
                cancel-text="取消"
                @confirm="removeBatchEntry(f)"
              >
                <a-button size="small" type="text" danger>移除</a-button>
              </a-popconfirm>
              <a-button v-else size="small" type="text" danger @click="removeBatchEntry(f)">移除</a-button>
            </div>

            <a-progress
              v-if="f.status === 'uploading' || f.status === 'processing'"
              :percent="f.progressPct"
              size="small"
              :show-info="true"
              style="margin-top:8px"
            />
            <div
              v-if="f.status === 'uploading' || f.status === 'processing'"
              class="batch-card__progress-detail"
            >
              当前：{{ f.stage }} · {{ f.progressPct }}%
              <span v-if="f.jobId"> · 任务 {{ f.jobId.slice(0, 8) }}</span>
            </div>
            <div
              v-if="f.jobId && (f.status === 'uploading' || f.status === 'processing' || f.status === 'done')"
              class="batch-card__steps"
            >
              <div
                v-for="(step, index) in BATCH_PROGRESS_STEPS"
                :key="step.key"
                class="batch-card__step"
                :class="`batch-card__step--${batchStepState(f, index)}`"
              >
                <span class="batch-card__step-dot">
                  <CheckOutlined v-if="batchStepState(f, index) === 'completed'" />
                  <CloseCircleOutlined v-else-if="batchStepState(f, index) === 'failed'" />
                  <span v-else>{{ index + 1 }}</span>
                </span>
                <span class="batch-card__step-label">{{ step.label }}</span>
                <span class="batch-card__step-pct">{{ step.pct }}%</span>
              </div>
            </div>

            <div v-if="f.error" style="color:#ff4d4f;font-size:12px;margin-top:4px">{{ f.error }}</div>

            <div v-if="f.status === 'done' && !f.confirmed" class="batch-card__body">
              <div class="batch-card__supplier-row">
                <span style="font-size:12px;color:rgba(0,0,0,0.55)">识别供应商：</span>
                <a-auto-complete
                  v-model:value="f.finalSupplierName"
                  style="width:220px"
                  size="small"
                  placeholder="供应商名称（可自由输入）"
                  :options="allSuppliers.map(s => ({ value: s.name, label: s.name, id: s.id }))"
                  :filter-option="(input: string, opt: { value?: unknown }) => String(opt.value ?? '').includes(input)"
                  @select="(_val: string, opt: { id?: number }) => { f.matchedSupplierId = opt.id ?? null }"
                  @change="(val: string) => {
                    const matched = allSuppliers.find(s => s.name === val)
                    if (!matched) f.matchedSupplierId = null
                    else f.matchedSupplierId = matched.id
                  }"
                />
                <a-tag v-if="f.matchedSupplierId" color="blue" style="margin-left:4px;font-size:11px">已关联</a-tag>
                <a-tag v-else style="margin-left:4px;font-size:11px">陌生</a-tag>
                <a-button type="primary" size="small" @click="confirmBatchEntry(f)">校对入库</a-button>
              </div>
            </div>
          </div>
        </div>
      </template>


      <!-- Legacy mode: per-supplier tabs -->
      <template v-else>
        <a-tabs :tab-position="'left'">
          <a-tab-pane
            v-for="s in selectedSuppliers"
            :key="s.id"
            :tab="`${s.name}${supplierUploads[s.id]?.confirmed ? ' ✓' : ''}`"
          >
            <div class="upload-pane">
              <div class="upload-pane__title">
                {{ s.name }} 报价单上传
                <a-tag v-if="supplierUploads[s.id]?.confirmed" color="green">已确认</a-tag>
              </div>

              <IntakeUploader
                v-if="!supplierUploads[s.id]?.confirmed"
                :type="'quote'"
                :context="{ supplier_id: s.id, project_id: taskConfig.projectId, category: taskConfig.category }"
                @extracted="(job) => onExtracted(s.id, job)"
              />

              <div v-if="(supplierUploads[s.id]?.items?.length ?? 0) > 0" style="margin-top:14px">
                <a-alert
                  type="info"
                  show-icon
                  message="识别完成，请核对后点击「校对入库」"
                  style="margin-bottom:10px"
                />
                <ExtractionEditor
                  schema="quote"
                  :model-value="supplierUploads[s.id]?.items as unknown[] as any"
                  :confirm-label="'校对入库'"
                  @confirm="() => confirmSupplier(s.id)"
                  @update:model-value="(v: any) => supplierUploads[s.id].items = v"
                />
              </div>

              <div v-else style="margin-top:14px;text-align:center">
                <a-button @click="skipSupplier(s.id)">
                  使用历史数据，跳过上传
                </a-button>
              </div>
            </div>
          </a-tab-pane>
        </a-tabs>
      </template>
    </a-card>

    <!-- Step 3: Alignment Gate -->
    <a-card v-else-if="currentStep === 3" :body-style="{ padding: '20px' }">
      <!-- Matching in progress -->
      <div v-if="matchRunning || tenderUploading" style="text-align:center;padding:48px 0">
        <a-spin size="large" />
        <div style="margin-top:14px;color:#666">正在运行嵌入匹配，请稍候...</div>
        <div style="margin-top:4px;font-size:12px;color:#999">通常 15~60 秒</div>
      </div>

      <!-- Review loaded -->
      <template v-else-if="anchorReviewResult">
        <!-- 多品类切换器 -->
        <div v-if="confirmedCategories.length > 1" style="margin:8px 0 4px;display:flex;align-items:center;gap:10px">
          <span style="font-size:12px;color:rgba(0,0,0,0.55)">品类：</span>
          <a-radio-group v-model:value="tenderCategory" button-style="solid" size="small">
            <a-radio-button v-for="c in confirmedCategories" :key="c" :value="c">{{ c }}</a-radio-button>
          </a-radio-group>
        </div>

        <!-- ② 采购清单对齐复核矩阵 -->
        <AnchorReviewMatrix
          v-if="taskConfig.projectId"
          :project-id="taskConfig.projectId"
          :category="tenderCategory || taskConfig.category"
          :submission-ids="effectiveSubmissionIds.length ? effectiveSubmissionIds : undefined"
          @pending-count="reviewPendingCount = $event"
        />
      </template>

      <div v-else-if="!matchRunning && !tenderUploading && !anchorReviewLoading" style="text-align:center;padding:48px 0;color:#999">
        正在初始化...
      </div>
      <div v-else-if="anchorReviewLoading" style="text-align:center;padding:32px 0">
        <a-spin />
        <div style="margin-top:10px;color:#666">加载复核数据...</div>
      </div>
    </a-card>

    <!-- Step 4: Results / Bid Matrix -->
    <template v-else-if="currentStep === STEP_RESULTS">
      <!-- Context tags -->
      <div v-if="matrixSummary" class="result-context">
        <a-tag color="default">{{ taskConfig.category }}</a-tag>
        <a-tag v-if="selectedProjectName" color="default">{{ selectedProjectName }}</a-tag>
      </div>

      <!-- ① Summary stat cards — hidden when anchor-matrix mode has its own metrics bar -->
      <div v-if="matrixSummary && !matrixCellStats" class="result-stats">
        <StatCard
          :icon="AppstoreOutlined"
          icon-bg="rgba(22,119,255,0.1)"
          label="比价材料"
          :value="matrixSummary.total_materials"
          unit="项"
        />
        <template v-if="isSingleSupplierMode">
          <StatCard
            :icon="TeamOutlined"
            icon-bg="rgba(114,46,209,0.1)"
            label="报价供应商"
            :value="matrixSuppliers[0]?.name ?? '—'"
          />
          <StatCard
            :icon="DollarOutlined"
            icon-bg="rgba(250,140,22,0.1)"
            label="报价总额"
            :value="'¥' + (matrixTotals[0]?.total ?? 0).toLocaleString()"
          />
          <StatCard
            :icon="LineChartOutlined"
            icon-bg="rgba(22,119,255,0.1)"
            label="平均偏差"
            :value="formatDeviation(matrixTotals[0]?.avg_deviation ?? 0)"
          />
        </template>
        <template v-else>
          <StatCard
            :icon="TeamOutlined"
            icon-bg="rgba(114,46,209,0.1)"
            label="参与供应商"
            :value="matrixSummary.total_suppliers"
            unit="家"
          />
          <StatCard
            :icon="TrophyOutlined"
            icon-bg="rgba(82,196,26,0.1)"
            label="价格优选候选人"
            :value="matrixSummary.price_preferred_candidate?.name ?? '—'"
          />
          <StatCard
            v-if="matrixSummary.allow_split && matrixSummary.optimal_total != null"
            :icon="DollarOutlined"
            icon-bg="rgba(250,140,22,0.1)"
            label="最优组合总价"
            :value="'¥' + matrixSummary.optimal_total.toLocaleString()"
          />
          <StatCard
            v-else
            :icon="DollarOutlined"
            icon-bg="rgba(250,140,22,0.1)"
            label="评标总价(价格优选)"
            :value="matrixSummary.price_preferred_total != null ? '¥' + Math.round(matrixSummary.price_preferred_total).toLocaleString() : '—'"
          />
        </template>
        <StatCard
          :icon="WarningOutlined"
          icon-bg="rgba(255,77,79,0.1)"
          label="异常项"
          :value="matrixSummary.anomaly_count"
          unit="项"
          :trend="matrixSummary.anomaly_count > 0 ? { value: '需关注', danger: true, label: '' } : undefined"
        />
      </div>

      <!-- ② Anchor-matrix cell accounting bar (Req4: 采购项×供应商 view) -->
      <div v-if="matrixCellStats" class="matrix-cell-bar">
        <div class="matrix-cell-bar__item">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.anchors }}</span>
          <span class="matrix-cell-bar__lbl">采购项</span>
        </div>
        <div class="matrix-cell-bar__item">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.supplier_count }}</span>
          <span class="matrix-cell-bar__lbl">供应商</span>
        </div>
        <div class="matrix-cell-bar__item">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.total_cells }}</span>
          <span class="matrix-cell-bar__lbl">单元格</span>
        </div>
        <div class="matrix-cell-bar__sep" />
        <div class="matrix-cell-bar__item matrix-cell-bar__item--ok">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.confirmed }}</span>
          <span class="matrix-cell-bar__lbl">已确认</span>
        </div>
        <div class="matrix-cell-bar__item" :class="matrixCellStats.pending > 0 ? 'matrix-cell-bar__item--warn' : 'matrix-cell-bar__item--ok'">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.pending }}</span>
          <span class="matrix-cell-bar__lbl">待确认</span>
        </div>
        <div class="matrix-cell-bar__item matrix-cell-bar__item--grey">
          <span class="matrix-cell-bar__val">{{ matrixCellStats.missing }}</span>
          <span class="matrix-cell-bar__lbl">未报价</span>
        </div>
        <div class="matrix-cell-bar__sep" />
        <div class="matrix-cell-bar__item matrix-cell-bar__item--blue">
          <span class="matrix-cell-bar__val">
            {{ matrixCellStats.quoted_ge_2 }}<span class="matrix-cell-bar__denom">/{{ matrixCellStats.anchors }}</span>
          </span>
          <span class="matrix-cell-bar__lbl">可比价(≥2家)</span>
        </div>
        <div class="matrix-cell-bar__item matrix-cell-bar__item--blue">
          <span class="matrix-cell-bar__val">
            {{ matrixCellStats.quoted_full }}<span class="matrix-cell-bar__denom">/{{ matrixCellStats.anchors }}</span>
          </span>
          <span class="matrix-cell-bar__lbl">{{ matrixCellStats.supplier_count }}家齐全</span>
        </div>
      </div>

      <!-- ③ Matrix table -->
      <a-card :body-style="{ padding: '0' }" style="margin-top:16px">
        <a-empty v-if="!analyzing && matrixRows.length === 0" description="当前条件下无可比数据" style="padding:40px 0" />
        <BidMatrix
          v-else
          :suppliers="matrixSuppliers"
          :rows="matrixRows"
          :totals="matrixTotals"
          :loading="analyzing"
          :category="taskConfig.category"
          :project-id="taskConfig.projectId"
          :supplier-ids="effectiveSupplierIds"
          :anchor-matrix="matrixResult?.anchor_matrix"
          :pending-item-loading="pendingItemLoading"
          @confirm-item="confirmPendingItem"
        />
      </a-card>

      <!-- ③ 评标结论横幅（三态：firm/conditional/blocked，始终展示） -->
      <a-alert
        v-if="matrixSummary && matrixResult && !isSingleSupplierMode"
        class="eval-banner"
        :type="matrixSummary.recommendation_level === 'blocked' ? 'error' : matrixSummary.recommendation_level === 'firm' ? 'success' : 'warning'"
        show-icon
        style="margin-top:16px"
      >
        <template #message>
          <span v-if="matrixSummary.recommendation_level === 'blocked'">无法形成评标总价排名（数据未达条件）</span>
          <span v-else-if="matrixSummary.recommendation_level === 'firm'">价格优选候选人：<strong>{{ matrixSummary.price_preferred_candidate?.name }}</strong></span>
          <span v-else>
            价格优选候选人（条件推荐）：<strong>{{ matrixSummary.price_preferred_candidate?.name || '—' }}</strong>
            <span v-if="matrixSummary.price_preferred_total != null">　评标总价 ¥{{ Math.round(matrixSummary.price_preferred_total).toLocaleString() }}</span>
          </span>
        </template>
        <template #description>
          <div style="font-size:12px;line-height:1.7">
            <div>评标方法：合理低价评标价法 — 最低报价不保证中标；本项目单一中标人，不做拆单组合。</div>
            <div v-if="matrixSummary.ranking?.length" style="margin-top:4px">
              评标总价排名：
              <span v-for="(r, i) in matrixSummary.ranking" :key="r.supplier_id">
                {{ i + 1 }}.{{ r.name }} ¥{{ Math.round(r.evaluated_total).toLocaleString() }}<span v-if="i < matrixSummary.ranking.length - 1">　</span>
              </span>
            </div>
            <ul v-if="matrixSummary.risks?.length" style="margin:6px 0 0;padding-left:18px;color:#8c8c8c">
              <li v-for="(rk, i) in matrixSummary.risks.slice(0, 6)" :key="i">{{ rk }}</li>
            </ul>
          </div>
        </template>
      </a-alert>

      <!-- ③ Supplier evaluation cards (multi-supplier only) -->
      <div v-if="matrixSummary && matrixResult && !isSingleSupplierMode" class="supplier-eval">
        <h3 class="section-title">供应商评标情况</h3>
        <a-row :gutter="[14, 14]">
          <a-col
            v-for="s in matrixSuppliers"
            :key="s.id"
            :xs="24" :sm="12" :lg="6"
          >
            <div
              class="eval-card"
              :class="{
                'eval-card--recommended': matrixSummary.price_preferred_candidate?.id === s.id,
              }"
            >
              <div class="eval-card__header">
                <span class="eval-card__badge">{{ s.letter }}</span>
                <div class="eval-card__name-block">
                  <span class="eval-card__name">{{ s.name }}</span>
                  <a-tag
                    v-if="matrixSummary.price_preferred_candidate?.id === s.id"
                    color="blue"
                    style="margin-left:6px;font-size:10px"
                  >★ 价格优选</a-tag>
                </div>
              </div>
              <div class="eval-card__metrics">
                <div class="eval-card__metric">
                  <span class="eval-card__metric-label">评标总价(含税)</span>
                  <span class="eval-card__metric-value">
                    ¥{{ (matrixTotals.find(t => t.supplier_id === s.id)?.evaluated_total ?? matrixTotals.find(t => t.supplier_id === s.id)?.total ?? 0).toLocaleString() }}
                  </span>
                </div>
                <div class="eval-card__metric">
                  <span class="eval-card__metric-label">平均偏差</span>
                  <span
                    class="eval-card__metric-value"
                    :style="{ color: normalizeAlert(
                      Math.abs(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) <= 0.05 ? 'normal'
                        : Math.abs(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) <= 0.1 ? 'yellow' : 'red'
                    ) === 'normal' ? '#52c41a' : normalizeAlert(
                      Math.abs(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) <= 0.05 ? 'normal'
                        : Math.abs(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) <= 0.1 ? 'yellow' : 'red'
                    ) === 'yellow' ? '#faad14' : '#ff4d4f' }"
                  >
                    {{ formatDeviation(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) }}
                  </span>
                </div>
                <div class="eval-card__metric">
                  <span class="eval-card__metric-label">报价完整度</span>
                  <span class="eval-card__metric-value">
                    {{ matrixTotals.find(t => t.supplier_id === s.id)?.quoted_count ?? 0 }}/{{ matrixRows.length }}
                  </span>
                </div>
                <div class="eval-card__metric">
                  <span class="eval-card__metric-label">异常项</span>
                  <span
                    class="eval-card__metric-value"
                    :style="{ color: (matrixTotals.find(t => t.supplier_id === s.id)?.anomaly_count ?? 0) > 0 ? '#ff4d4f' : '#52c41a' }"
                  >
                    {{ matrixTotals.find(t => t.supplier_id === s.id)?.anomaly_count ?? 0 }}
                  </span>
                </div>
              </div>
              <div class="eval-card__tags">
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.quoted_count ?? 0) === matrixRows.length" color="green">报价完整</a-tag>
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.tax_assumed_lines ?? 0) > 0" color="orange">
                  税口径假定含税 {{ matrixTotals.find(t => t.supplier_id === s.id)?.tax_assumed_lines }} 行
                </a-tag>
                <a-tag v-else-if="matrixTotals.find(t => t.supplier_id === s.id)?.basis_confirmed === false" color="orange">税口径待确认</a-tag>
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.undecided_lines ?? 0) > 0" color="gold">
                  {{ matrixTotals.find(t => t.supplier_id === s.id)?.undecided_lines }} 行未决
                </a-tag>
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.qty_conflict_lines ?? 0) > 0" color="purple">
                  {{ matrixTotals.find(t => t.supplier_id === s.id)?.qty_conflict_lines }} 行数量冲突
                </a-tag>
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.anomaly_count ?? 0) === 0" color="cyan">无异常</a-tag>
              </div>
            </div>
          </a-col>
        </a-row>
      </div>

      <!-- ④ AI insight -->
      <a-card v-if="insightLoading || insightResult" class="insight-card" style="margin-top:16px">
        <template #title>
          <span style="display:flex;align-items:center;gap:8px">
            <RobotOutlined style="color:#1677ff" />
            <span style="font-size:15px;font-weight:600">AI 综合分析建议</span>
          </span>
        </template>
        <a-spin :spinning="insightLoading" tip="正在分析比价数据...">
          <template v-if="insightResult && !insightResult.error">
            <div v-if="insightResult.overall" class="insight-section">
              <h4 class="insight-section__title">
                <BulbOutlined style="color:#faad14" /> 整体评估
              </h4>
              <p class="insight-section__text">{{ insightResult.overall }}</p>
            </div>
            <div v-if="insightResult.recommendations?.length" class="insight-section">
              <h4 class="insight-section__title">
                <CheckCircleOutlined style="color:#52c41a" /> 评标解读（仅解释系统结果，非定标结论）
              </h4>
              <ul class="insight-section__list">
                <li v-for="(rec, i) in insightResult.recommendations" :key="i">{{ rec }}</li>
              </ul>
            </div>
            <div v-if="insightResult.risks?.length" class="insight-section">
              <h4 class="insight-section__title">
                <WarningOutlined style="color:#ff4d4f" /> 风险提示
              </h4>
              <ul class="insight-section__list insight-section__list--risk">
                <li v-for="(risk, i) in insightResult.risks" :key="i">{{ risk }}</li>
              </ul>
            </div>
          </template>
          <a-empty v-else-if="insightResult?.error" :description="insightResult.error" />
          <div v-else style="min-height:60px" />
        </a-spin>
      </a-card>

      <!-- ⑤ Bottom action bar -->
      <div class="result-bottom-bar">
        <div class="result-bottom-bar__info">
          <template v-if="matrixSummary && isSingleSupplierMode">
            <span class="result-bottom-bar__total">
              报价总额：<strong>¥{{ (matrixTotals[0]?.total ?? 0).toLocaleString() }}</strong>
            </span>
            <a-tag :color="matrixSummary.anomaly_count > 0 ? 'red' : 'green'" style="margin-left:8px">
              {{ matrixSummary.anomaly_count > 0 ? `${matrixSummary.anomaly_count} 项偏差较大` : '价格正常' }}
            </a-tag>
          </template>
          <template v-else-if="matrixSummary">
            <!-- 拆单最优组合总价：仅招标文件允许分项授标时展示 -->
            <template v-if="matrixSummary.allow_split && matrixSummary.optimal_total != null">
              <span class="result-bottom-bar__total">
                最优组合总价：<strong>¥{{ matrixSummary.optimal_total.toLocaleString() }}</strong>
              </span>
              <a-tag v-if="savingsPercent" color="green" style="margin-left:8px">节省 {{ savingsPercent }}%</a-tag>
            </template>
            <!-- 单一授标：展示价格优选候选人评标总价（条件推荐，需委员会确认） -->
            <template v-else>
              <span class="result-bottom-bar__total">
                价格优选候选人：<strong>{{ matrixSummary.price_preferred_candidate?.name || '—' }}</strong>
                <span v-if="matrixSummary.price_preferred_total != null">　评标总价 ¥{{ Math.round(matrixSummary.price_preferred_total).toLocaleString() }}</span>
              </span>
              <a-tag color="orange" style="margin-left:8px">条件推荐 · 需招标领导小组确认</a-tag>
            </template>
          </template>
        </div>
        <a-space>
          <a-button @click="goBack">
            <template #icon><LeftOutlined /></template>
            返回核查
          </a-button>
          <a-button @click="runMatrix">
            <template #icon><LineChartOutlined /></template>
            重新比价
          </a-button>
          <a-button
            type="primary"
            ghost
            :loading="matrixSaving"
            :disabled="matrixSaving || savedMatrixVersionId !== null || alignmentFinalizationId === null"
            @click="saveMatrixVersion"
          >
            保存本版比价
          </a-button>
          <template v-if="savedMatrixVersionId">
            <a-tag color="blue">v{{ savedMatrixVersionId }}</a-tag>
            <a-button
              type="primary"
              :loading="matrixApproving"
              :disabled="matrixApproving || matrixApproved"
              @click="approveMatrixVersion"
            >
              {{ matrixApproved ? '已审批通过' : '审批通过' }}
            </a-button>
          </template>
        </a-space>
      </div>
    </template>

    <!-- Footer nav (before results) -->
    <div v-if="currentStep < STEP_RESULTS" class="compare-page__footer">
      <a-button v-if="currentStep > 0" @click="goBack" :disabled="matchRunning || tenderUploading">
        <template #icon><LeftOutlined /></template>
        上一步
      </a-button>

      <!-- Step 3: gate button -->
      <template v-if="currentStep === 3">
        <span v-if="!allPendingActioned && reviewPendingCount !== null && reviewPendingCount > 0" style="font-size:12px;color:#faad14;margin-right:4px">
          还有 {{ reviewPendingCount }} 条待确认
        </span>
        <!-- finalize: still requires pending=0 (backend hard gate) -->
        <a-button
          :loading="alignmentFinalizing"
          :disabled="!allPendingActioned || anchorReviewLoading || matchRunning || alignmentFinalizationId !== null"
          @click="finalizeAlignment"
          style="margin-right:6px"
        >
          完成对齐审核
        </a-button>
        <a-tag v-if="alignmentFinalizationId" color="green" style="margin-right:8px">已锁定</a-tag>
        <!-- generate matrix: no longer blocked by pending (v2.5) -->
        <a-button
          type="primary"
          :loading="analyzing"
          :disabled="anchorReviewLoading || matchRunning"
          @click="goNext"
        >
          生成比价矩阵
          <template #icon><RightOutlined /></template>
        </a-button>
      </template>

      <a-button v-else type="primary" :loading="matchRunning || tenderUploading || tenderListConfirming" @click="goNext">
        <template v-if="currentStep === 2">开始匹配</template>
        <template v-else>下一步</template>
        <template #icon><RightOutlined /></template>
      </a-button>
    </div>

    <!-- New Project Modal -->
    <a-modal
      v-model:open="newProjectVisible"
      title="新建项目"
      :confirm-loading="newProjectSaving"
      ok-text="创建"
      cancel-text="取消"
      @ok="handleCreateProject"
      :width="520"
    >
      <a-form layout="vertical" style="margin-top:16px">
        <a-form-item label="项目名称" required>
          <a-input v-model:value="newProjectForm.name" placeholder="例：XX 项目二期" :maxlength="100" />
        </a-form-item>
        <a-form-item label="项目编号">
          <a-input v-model:value="newProjectForm.code" placeholder="例：PRJ-2026-001（可留空）" :maxlength="50" />
        </a-form-item>
        <a-form-item label="项目地址">
          <a-input v-model:value="newProjectForm.location" placeholder="例：上海市浦东新区 XX 路" :maxlength="200" />
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="newProjectForm.remark" placeholder="可选备注信息" :rows="2" :maxlength="500" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.compare-page {
  &__header { margin-bottom: 16px; }
  &__title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: @heading-color;
  }
  &__subtitle {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }
  &__footer {
    margin-top: 20px;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
}

.upload-pane {
  &__title {
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 12px;
    color: @heading-color;
  }
}

.batch-overall {
  margin-top: 12px;
  padding: 8px 12px;
  background: #f6f8fa;
  border-radius: @border-radius-base;
  display: flex;
  align-items: center;
  font-size: 13px;
  color: @text-color-secondary;
}

.batch-list-header {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;

  &__count {
    font-size: 13px;
    color: @text-color-secondary;
  }
}

.batch-list {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.batch-card {
  border: 1px solid @border-color-base;
  border-radius: @border-radius-base;
  padding: 12px 14px;
  background: #fff;
  transition: border-color 0.2s;

  &--done {
    border-color: #b7eb8f;
    background: #f6ffed;
  }

  &__head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__filename {
    font-size: 13px;
    font-weight: 500;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__body { margin-top: 8px; }

  &__progress-detail {
    margin-top: 4px;
    font-size: 12px;
    color: @text-color-secondary;
  }

  &__steps {
    margin-top: 10px;
    display: grid;
    grid-template-columns: repeat(8, minmax(72px, 1fr));
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  &__step {
    min-width: 72px;
    border: 1px solid @border-color-split;
    border-radius: 6px;
    padding: 7px 6px;
    background: #fafafa;
    display: grid;
    grid-template-columns: 18px 1fr;
    column-gap: 5px;
    row-gap: 1px;
    align-items: center;
  }

  &__step-dot {
    grid-row: span 2;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: #d9d9d9;
    color: #fff;
    font-size: 10px;
    line-height: 1;
  }

  &__step-label {
    font-size: 12px;
    color: @text-color;
    white-space: nowrap;
  }

  &__step-pct {
    font-size: 11px;
    color: @text-color-secondary;
  }

  &__step--completed {
    border-color: #b7eb8f;
    background: #f6ffed;

    .batch-card__step-dot { background: #52c41a; }
    .batch-card__step-label { color: #237804; }
  }

  &__step--active {
    border-color: #91caff;
    background: #e6f4ff;
    box-shadow: 0 0 0 1px rgba(22, 119, 255, 0.12);

    .batch-card__step-dot { background: #1677ff; }
    .batch-card__step-label {
      color: #0958d9;
      font-weight: 600;
    }
  }

  &__step--failed {
    border-color: #ffa39e;
    background: #fff1f0;

    .batch-card__step-dot { background: #ff4d4f; }
    .batch-card__step-label { color: #a8071a; }
  }

  &__supplier-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
}

/* ─── Pending gate ──────────────────────────────────────────────────── */

.pending-gate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pending-gate-item {
  border: 1px solid #ffd591;
  border-radius: @border-radius-base;
  background: #fffbe6;
  padding: 12px 14px;

  &__head {
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }

  &__desc {
    flex: 1;
    min-width: 0;
  }

  &__anchor {
    font-size: 13px;
    color: @heading-color;
    display: block;
  }

  &__sups {
    margin-top: 6px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  &__sup-tag {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: rgba(0,0,0,0.55);
    background: #fff;
    border: 1px solid #d9d9d9;
    border-radius: 4px;
    padding: 2px 8px;
  }
}

/* ─── Result page sections ──────────────────────────────────────────── */

.result-context {
  margin-bottom: 12px;
}

.result-stats {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;

  > * {
    flex: 1;
    min-width: 160px;
  }
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: @heading-color;
  margin: 0 0 14px;
}

.supplier-eval {
  margin-top: 20px;
}

.eval-card {
  background: #fff;
  border: 1px solid @border-color-split;
  border-radius: @border-radius-lg;
  padding: 16px;
  transition: all 0.2s;

  &:hover {
    box-shadow: @shadow-1;
  }

  &--recommended {
    border-color: #1677ff;
    box-shadow: 0 0 0 1px rgba(22, 119, 255, 0.15);
  }

  &__header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 14px;
  }

  &__badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: @primary-color;
    color: #fff;
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
  }

  &__name-block {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    min-width: 0;
  }

  &__name {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__metrics {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }

  &__metric {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__metric-label {
    font-size: 12px;
    color: @text-color-secondary;
  }

  &__metric-value {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
  }

  &__tags {
    margin-top: 12px;
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }
}

.insight-card {
  :deep(.ant-card-head) {
    border-bottom: 1px solid @border-color-split;
  }
}

.insight-section {
  &:not(:last-child) {
    margin-bottom: 16px;
    padding-bottom: 16px;
    border-bottom: 1px dashed @border-color-split;
  }

  &__title {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
    margin: 0 0 8px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  &__text {
    font-size: 13px;
    color: @text-color;
    line-height: 1.7;
    margin: 0;
  }

  &__list {
    margin: 0;
    padding-left: 20px;
    font-size: 13px;
    color: @text-color;
    line-height: 1.8;

    li::marker {
      color: #52c41a;
    }

    &--risk li::marker {
      color: #ff4d4f;
    }
  }
}

/* ─── Tender upload section ────────────────────────────────────────── */
.tender-upload {
  padding: 12px 14px;
  background: #f6ffed;
  border: 1px solid #b7eb8f;
  border-radius: @border-radius-base;

  &__header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
  }

  &__title {
    font-size: 13px;
    font-weight: 600;
    color: @heading-color;
  }

  &__hint {
    font-size: 12px;
    color: rgba(0, 0, 0, 0.45);
  }

  &__action {
    display: flex;
    align-items: center;
  }

  &__result {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
}

.tender-stats {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.tender-stat {
  font-size: 13px;
  color: @text-color;

  strong { font-weight: 600; color: @heading-color; }
  em { font-style: normal; color: rgba(0, 0, 0, 0.45); margin-left: 2px; }

  &--warn {
    color: #d48806;
    display: flex;
    align-items: center;
    gap: 4px;
    strong { color: #d48806; }
  }
}

.tender-stat-sep {
  color: rgba(0, 0, 0, 0.2);
  font-size: 13px;
}

/* ─── Anchor Review step ────────────────────────────────────────────── */
.llm-fill-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #adc6ff;

  &__hint {
    font-size: 12px;
    color: rgba(0, 0, 0, 0.45);
  }
}

.llm-fill-result {
  margin-top: 16px;
  background: #f9f0ff;
  border: 1px solid #d3adf7;
  border-radius: @border-radius-base;
  padding: 14px 16px;

  &__title {
    font-size: 14px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
}

.anchor-summary {
  background: #f0f5ff;
  border: 1px solid #adc6ff;
  border-radius: @border-radius-base;
  padding: 14px 16px;

  &__title {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 12px;
  }

  &__stats {
    display: flex;
    gap: 0;
    flex-wrap: wrap;
  }
}

.anchor-stat {
  flex: 1;
  min-width: 90px;
  padding: 8px 12px;
  border-right: 1px solid #d6e4ff;
  text-align: center;

  &:last-child { border-right: none; }

  &__value {
    font-size: 22px;
    font-weight: 700;
    color: @heading-color;
    line-height: 1.2;
  }

  &__denom {
    font-size: 14px;
    font-weight: 400;
    color: rgba(0, 0, 0, 0.4);
  }

  &__label {
    font-size: 11px;
    color: rgba(0, 0, 0, 0.5);
    margin-top: 2px;
  }

  &--highlight &__value { color: #1677ff; }
  &--ok &__value { color: #52c41a; }
  &--warn &__value { color: #faad14; }
}

.lc-group-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lc-group-name {
  font-size: 14px;
  font-weight: 500;
  color: @heading-color;
}

.lc-group-spec {
  font-size: 12px;
  color: rgba(0, 0, 0, 0.5);
}

.result-bottom-bar {
  margin-top: 20px;
  padding: 14px 20px;
  background: #fff;
  border: 1px solid @border-color-split;
  border-radius: @border-radius-lg;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  bottom: 0;
  z-index: 10;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06);

  &__info {
    display: flex;
    align-items: center;
  }

  &__total {
    font-size: 15px;
    color: @text-color;

    strong {
      font-size: 20px;
      color: @primary-color;
    }
  }
}

/* ─── Anchor matrix cell-accounting bar (Req4) ──────────────────────── */
.matrix-cell-bar {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 10px 0;
  margin-top: 16px;
  background: #fafafa;
  border: 1px solid @border-color-split;
  border-radius: @border-radius-base;
  flex-wrap: wrap;

  &__item {
    flex: 1;
    min-width: 80px;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 4px 12px;
    border-right: 1px solid @border-color-split;

    &:last-child { border-right: none; }
    &--ok .matrix-cell-bar__val { color: #52c41a; }
    &--warn .matrix-cell-bar__val { color: #faad14; }
    &--grey .matrix-cell-bar__val { color: #8c8c8c; }
    &--blue .matrix-cell-bar__val { color: #1677ff; }
  }

  &__val {
    font-size: 20px;
    font-weight: 700;
    line-height: 1.2;
    color: @heading-color;
  }

  &__denom {
    font-size: 12px;
    font-weight: 400;
    color: rgba(0, 0, 0, 0.4);
  }

  &__lbl {
    font-size: 11px;
    color: rgba(0, 0, 0, 0.45);
    margin-top: 1px;
  }

  &__sep {
    width: 1px;
    height: 40px;
    background: @border-color-base;
    flex-shrink: 0;
    margin: 0 4px;
  }
}
</style>
