<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckCircleOutlined, LineChartOutlined, RightOutlined, LeftOutlined,
  CloudUploadOutlined, LoadingOutlined, CheckOutlined, CloseCircleOutlined,
  PlusOutlined,
  AppstoreOutlined, TeamOutlined, TrophyOutlined, DollarOutlined,
  WarningOutlined, BulbOutlined, RobotOutlined,
  FileExcelOutlined, AimOutlined,
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
  LlmFillResult,
} from '@/api/client'
import IntakeUploader from '@/components/IntakeUploader.vue'
import ExtractionEditor from '@/components/ExtractionEditor.vue'
import StatCard from '@/components/StatCard.vue'
import BidMatrix from './components/BidMatrix.vue'
import AnchorReviewMatrix from './components/AnchorReviewMatrix.vue'
import { normalizeAlert, formatDeviation } from '@/utils/alert'
import { asQuoteShape } from '@/utils/extraction'

const PROFESSION_CATEGORIES: Record<string, string[]> = {
  '电气': ['桥架', '母线槽', '配电箱'],
  '给排水': ['阀门', '不锈钢管', '水箱', '潜水泵'],
  '暖通': ['风口风阀', '风机盘管', '空调泵'],
}
// Steps: 0=config, 1=procurement list, 2=supplier quotes, 3=alignment review, 4=matrix
const STEP_RESULTS = 4

// ─── State ───────────────────────────────────────────────────────────────
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

// ─── Step 3: Alignment finalization gate ─────────────────────────────────
const alignmentFinalizing = ref(false)
const alignmentFinalizationId = ref<number | null>(null)

// ─── Step 4: Matrix save / approve gate ──────────────────────────────────
const matrixSaving = ref(false)
const savedMatrixVersionId = ref<number | null>(null)
const matrixApproving = ref(false)
const matrixApproved = ref(false)

async function previewTenderList(file: File) {
  tenderPreviewing.value = true
  tenderPreview.value = null
  const form = new FormData()
  form.append('file', file)
  try {
    const { data } = await analysisApi.tenderListPreview(form)
    tenderFile.value = file
    tenderPreview.value = data
    // 单品类自动选多数派；多品类也先选多数派作为当前处理品类
    tenderCategory.value = data.detected_category || taskConfig.category || ''
    if (data.has_multiple_categories) {
      const parts = Object.entries(data.category_breakdown)
        .map(([c, n]) => `${c}×${n}`).join('、')
      message.info(`检测到多个品类：${parts}，确认后将按品类拆分`)
    } else {
      message.success(`采购清单已解析：${data.total} 条采购项 · 品类：${data.detected_category || '待确认'}`)
    }
    if (data.unknown_count > 0) {
      message.warning(`有 ${data.unknown_count} 项无法自动识别品类，请在表中核对或手动选择品类`)
    }
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '解析失败'
    message.error(detail)
  } finally {
    tenderPreviewing.value = false
  }
}

async function confirmTenderListVersion() {
  if (!tenderPreview.value) return
  const hasUnknown = (tenderPreview.value.unknown_count ?? 0) > 0
  if (hasUnknown && !forceUnknownCategory.value) {
    message.warning('存在未识别品类的采购项，请勾选「强制归入默认品类」后再确认')
    return
  }
  tenderListConfirming.value = true
  try {
    const { data } = await analysisApi.tenderListConfirm({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
      file_name: tenderFile.value?.name ?? '',
      anchors_total: tenderPreview.value.total,
      anchors_json: tenderPreview.value.items,
      force: hasUnknown && forceUnknownCategory.value,
    })
    // 记录已确认的品类 + 构建 categorySessionMap(切换品类时同步 session_id)
    confirmedCategories.value = (data.sessions || []).map(s => s.category)
    categorySessionMap.value = Object.fromEntries(
      (data.sessions || []).map(s => [s.category, s.id])
    )
    // 当前品类对应的 session id
    const curCat = tenderCategory.value || taskConfig.category
    tenderListSessionId.value = categorySessionMap.value[curCat] ?? data.id
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
      recommended_supplier: matrixSummary.value?.recommended_supplier?.name ?? '',
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
const pendingGroupLoading = ref<Record<number, boolean>>({})
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

/** Group-level bulk: confirm entire group (all pending items → align) */
async function confirmPendingGroup(groupId: number, action: 'confirm' | 'reject') {
  pendingGroupLoading.value[groupId] = true
  try {
    await analysisApi.anchorReviewConfirm({ group_id: groupId, action })
    await loadAnchorReview()
    message.success(action === 'confirm' ? '已批量确认整组' : '已移除整组')
  } catch {
    message.error('操作失败，请重试')
  } finally {
    pendingGroupLoading.value[groupId] = false
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
  detectedSupplierName: string
  matchedSupplierId: number | null  // auto-matched
  items: QuoteExtractionItem[]
  confirmedSupplierId: number | null
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
  const rows = matrixResult.value.rows
  const totals = matrixResult.value.totals
  const suppliers = matrixResult.value.suppliers
  const best = totals.length
    ? totals.reduce((a, b) => (a.avg_deviation < b.avg_deviation ? a : b))
    : null
  const bestSupplier = best ? suppliers.find((s) => s.id === best.supplier_id) : null
  // Optimal total: sum of min prices per row
  const optimalTotal = rows.reduce((sum, row) => {
    const tots = row.suppliers.filter((c) => c.total !== null).map((c) => c.total as number)
    return sum + (tots.length ? Math.min(...tots) : 0)
  }, 0)
  const anomalyCount = rows.reduce(
    (n, r) => n + r.suppliers.filter((c) => c.alert_level === 'red').length, 0,
  )
  return {
    total_materials: rows.length,
    total_suppliers: suppliers.length,
    recommended_supplier: bestSupplier,
    optimal_total: Math.round(optimalTotal),
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
  insightLoading.value = true
  insightResult.value = null
  try {
    // Truncate rows to keep request body small — backend prompt also limits to 30 rows
    const trimmed: BidMatrixResult = {
      ...matrixResult.value,
      rows: matrixResult.value.rows.slice(0, 50),
    }
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

// Savings percentage for bottom bar
const savingsPercent = computed(() => {
  if (!matrixSummary.value || !matrixResult.value) return null
  const totals = matrixResult.value.totals
  if (totals.length < 2) return null
  const avgTotal = totals.reduce((s, t) => s + t.total, 0) / totals.length
  if (avgTotal <= 0) return null
  const ratio = 1 - matrixSummary.value.optimal_total / avgTotal
  return ratio > 0 ? (ratio * 100).toFixed(1) : null
})

// ─── Tender List / Anchor matching ──────────────────────────────────────
const tenderMatchSummary = ref<AnchorMatchSummary | null>(null)

// ─── LLM 供应商视角填表(replace) ──────────────────────────────────────────
const llmFilling = ref(false)
const llmFillResult = ref<LlmFillResult | null>(null)
const llmFillColumns = [
  { title: '供应商', dataIndex: 'supplier_name', key: 'supplier_name' },
  { title: '已填', dataIndex: 'quoted', key: 'quoted', align: 'right' as const },
  { title: '聚合', dataIndex: 'aggregated', key: 'aggregated', align: 'right' as const },
  { title: '待审', dataIndex: 'pending', key: 'pending', align: 'right' as const },
  { title: '排除', dataIndex: 'excluded', key: 'excluded', align: 'right' as const },
  { title: '清单外', dataIndex: 'residue', key: 'residue', align: 'right' as const },
  { title: '清单外(高相似)', dataIndex: 'residue_high_cos', key: 'residue_high_cos', align: 'right' as const },
  { title: '丢弃', dataIndex: 'dropped', key: 'dropped', align: 'right' as const },
  { title: '状态', key: 'error', align: 'center' as const },
]

async function runLlmFill() {
  if (!taskConfig.projectId) return
  if (effectiveSupplierIds.value.length === 0) {
    message.error('LLM 填表需要供应商报价范围，请先完成报价上传匹配')
    return
  }
  llmFilling.value = true
  try {
    const sids = effectiveSupplierIds.value
    const { data } = await analysisApi.tenderListLlmFill({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
      supplier_ids: sids.length ? sids : undefined,
      tender_list_session_id: tenderListSessionId.value ?? undefined,
      mode: 'replace',
    })
    llmFillResult.value = data
    // replace 改写了对齐组 → 旧 finalization/矩阵版本已失效，必须重新 finalize
    if (data.finalization_invalidated || alignmentFinalizationId.value !== null) {
      alignmentFinalizationId.value = null
      savedMatrixVersionId.value = null
      matrixApproved.value = false
      message.warning('LLM 填表已重写对齐结果，请重新「完成对齐审核」后再保存比价版本')
    }
    const delta = data.comparable_2plus - data.comparable_2plus_embedding_baseline
    message.success(
      `LLM 填表完成：可比≥2 ${data.comparable_2plus}/${data.anchors_total}` +
      `（embedding 基线 ${data.comparable_2plus_embedding_baseline}，` +
      `${delta >= 0 ? '+' : ''}${delta}）`,
    )
    await loadAnchorReview()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail ?? 'LLM 填表失败')
  } finally {
    llmFilling.value = false
  }
}
const tenderUploading = ref(false)
const anchorReviewResult = ref<AnchorReviewResult | null>(null)
const anchorReviewLoading = ref(false)

// Run tender list matching (called when entering Step 3)
async function runTenderMatch(): Promise<boolean> {
  if (!tenderFile.value || !taskConfig.projectId) return false
  tenderUploading.value = true
  const form = new FormData()
  form.append('file', tenderFile.value)
  form.append('project_id', String(taskConfig.projectId))
  const cat = tenderCategory.value || taskConfig.category
  if (cat) form.append('category', cat)
  const sids = effectiveSupplierIds.value
  if (sids.length) form.append('supplier_ids', sids.join(','))
  try {
    const { data } = await analysisApi.tenderListMatch(form)
    tenderMatchSummary.value = data
    if (cat && !taskConfig.category) taskConfig.category = cat
    return true
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '招标清单匹配失败'
    message.error(detail)
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
    const { data } = await analysisApi.anchorReview({
      project_id: taskConfig.projectId,
      category: tenderCategory.value || taskConfig.category,
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
  if (sids.length === 0) {
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

// Single-supplier mode: compare against history instead of across suppliers
const isSingleSupplierMode = computed(() => effectiveSupplierIds.value.length === 1)

// Effective supplier IDs for BidMatrix export
const effectiveSupplierIds = computed(() => {
  if (useBatchMode.value) {
    return [...new Set(batchFiles.value.filter(f => f.confirmed && f.confirmedSupplierId).map(f => f.confirmedSupplierId!))]
  }
  return taskConfig.supplierIds
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

onMounted(() => {
  fetchProjects()
  fetchSuppliers()
})

// Initialise + clean up upload slots when supplier selection changes.
// AUDIT-FIX M1: previously we only ADDED entries — unchecking and re-checking
// a supplier kept the prior confirmed=true state, making bid-matrix include
// stale uploads.
// 切换品类时同步 session_id(多品类场景：confirm 后 categorySessionMap 已填充)
watch(tenderCategory, (cat) => {
  if (cat && categorySessionMap.value[cat]) {
    tenderListSessionId.value = categorySessionMap.value[cat]
  }
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
    if (!tenderPreview.value) {
      message.warning('请先上传采购清单')
      return
    }
    if (!tenderCategory.value) {
      message.warning('请确认品类')
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
    if (effectiveSupplierIds.value.length === 0) {
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
  try {
    const { data } = await quoteApi.batchConfirm({
      job_id: slot.job.id,
      supplier_id: supplierId,
      project_id: taskConfig.projectId,
      category: taskConfig.category,
      overrides: slot.items as unknown as Array<Record<string, unknown>>,
      bid_status: taskConfig.bidStatus,
    })
    const result = data as BatchConfirmResult
    slot.confirmed = true
    slot.batch_id = result.batch_id
    message.success(`已入库 ${result.created} 条报价`)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '入库失败'
    message.error(detail)
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
      matchedSupplierId: null,
      items: [],
      confirmedSupplierId: null,
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
  // Auto-match against known suppliers
  if (entry.detectedSupplierName) {
    const name = entry.detectedSupplierName.replace(/\s/g, '').toLowerCase()
    const match = allSuppliers.value.find(
      (s) => s.name.replace(/\s/g, '').toLowerCase() === name
        || s.name.includes(entry.detectedSupplierName)
        || entry.detectedSupplierName.includes(s.name)
    )
    if (match) {
      entry.matchedSupplierId = match.id
    }
  }
}

async function confirmBatchEntry(entry: BatchFileEntry) {
  if (!entry.jobId) return
  const supplierName = entry.matchedSupplierId
    ? allSuppliers.value.find((s) => s.id === entry.matchedSupplierId)?.name || entry.detectedSupplierName
    : entry.detectedSupplierName
  if (!supplierName) {
    message.warning('请输入或选择供应商名称')
    return
  }
  try {
    const { data } = await quoteApi.batchConfirm({
      job_id: entry.jobId,
      supplier_id: entry.matchedSupplierId ?? undefined,
      supplier_name: supplierName,
      project_id: taskConfig.projectId,
      category: taskConfig.category,
      overrides: entry.items as unknown as Array<Record<string, unknown>>,
      bid_status: taskConfig.bidStatus,
    })
    entry.confirmed = true
    entry.confirmedSupplierId = data.supplier_id ?? null
    message.success(`${supplierName}：已入库 ${data.created} 条报价`)
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
      const detail = typeof resp === 'string' ? resp : '入库失败'
      message.error(detail)
    }
  }
}

function removeBatchEntry(entry: BatchFileEntry) {
  if (entry.pollTimer) clearInterval(entry.pollTimer)
  batchFiles.value = batchFiles.value.filter((f) => f.id !== entry.id)
}

onBeforeUnmount(() => {
  for (const f of batchFiles.value) {
    if (f.pollTimer) clearInterval(f.pollTimer)
  }
})

// ─── Step 4: run bid-matrix ──────────────────────────────────────────────
async function runMatrix() {
  // Gather supplier IDs: from pre-selected OR from batch confirmed entries
  const sids = useBatchMode.value
    ? [...new Set(batchFiles.value.filter((f) => f.confirmed && f.confirmedSupplierId).map((f) => f.confirmedSupplierId!))]
    : taskConfig.supplierIds
  if (sids.length < 1) {
    message.warning('至少需要 1 家供应商的报价才能比价')
    return
  }
  analyzing.value = true
  matrixResult.value = null
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: taskConfig.projectId,
      supplier_ids: sids,
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
      <a-step title="采购清单" description="上传 Excel，预览内容" />
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

    <!-- Step 1: Upload Procurement List -->
    <a-card v-else-if="currentStep === 1" :body-style="{ padding: '20px' }">
      <div v-if="!tenderPreview">
        <div style="margin-bottom:20px">
          <h3 style="margin:0 0 6px;font-size:15px;font-weight:600">上传采购清单</h3>
          <div style="font-size:12px;color:rgba(0,0,0,0.45)">
            上传工程量清单（.xlsx），系统解析品名/规格/数量，作为比价骨架
          </div>
        </div>
        <a-upload-dragger
          accept=".xlsx,.xls"
          :show-upload-list="false"
          :before-upload="(f: File) => { previewTenderList(f); return false; }"
        >
          <p class="ant-upload-drag-icon">
            <FileExcelOutlined style="color:#52c41a;font-size:36px" />
          </p>
          <p class="ant-upload-text">点击或拖入采购清单 Excel</p>
          <p class="ant-upload-hint">支持 .xlsx / .xls · 自动识别品名、规格、数量、单位</p>
        </a-upload-dragger>
        <div v-if="tenderPreviewing" style="text-align:center;padding:32px 0">
          <a-spin size="large" />
          <div style="margin-top:12px;color:#666">正在解析...</div>
        </div>
      </div>

      <div v-else>
        <!-- Detected category + breakdown + edit -->
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;padding:12px;background:#f6f8fa;border-radius:6px">
          <FileExcelOutlined style="color:#52c41a;font-size:18px;flex-shrink:0" />
          <div style="flex:1">
            <div style="font-weight:600;font-size:14px">{{ tenderFile?.name }}</div>
            <div style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:2px">
              共 {{ tenderPreview.total }} 条采购项
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:12px;color:rgba(0,0,0,0.55);white-space:nowrap">
              {{ tenderPreview.has_multiple_categories ? '当前品类：' : '品类：' }}
            </span>
            <a-select
              v-model:value="tenderCategory"
              style="width:160px"
              placeholder="请选择品类"
              show-search
              allow-clear
            >
              <a-select-option v-for="c in Object.values(PROFESSION_CATEGORIES).flat()" :key="c" :value="c">{{ c }}</a-select-option>
            </a-select>
          </div>
          <a-button size="small" type="link" @click="tenderPreview = null; tenderFile = null">重新上传</a-button>
        </div>

        <!-- Category breakdown bar -->
        <div
          v-if="Object.keys(tenderPreview.category_breakdown || {}).length || tenderPreview.unknown_count"
          style="display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap;font-size:12px"
        >
          <span style="color:rgba(0,0,0,0.55)">识别到品类：</span>
          <a-tag
            v-for="(n, c) in tenderPreview.category_breakdown"
            :key="c"
            :color="c === tenderCategory ? 'blue' : 'default'"
            style="cursor:pointer"
            @click="tenderCategory = String(c)"
          >{{ c }} × {{ n }}</a-tag>
          <a-tag v-if="tenderPreview.unknown_count" color="orange">未识别 × {{ tenderPreview.unknown_count }}</a-tag>
          <span v-if="tenderPreview.has_multiple_categories" style="color:#fa8c16">
            · 多品类将按品类各自拆分为独立采购清单
          </span>
        </div>

        <!-- Preview table -->
        <a-table
          :data-source="tenderPreview.items"
          :row-key="(r: Record<string,unknown>) => String(r.seq)"
          size="small"
          :pagination="{ pageSize: 10, size: 'small' }"
          :scroll="{ x: 700 }"
          :columns="[
            { title: '序号', dataIndex: 'seq', width: 60 },
            { title: '品名', dataIndex: 'name', ellipsis: true },
            { title: '规格', dataIndex: 'spec', width: 180, ellipsis: true },
            { title: '专业', dataIndex: 'profession', key: 'profession', width: 80 },
            { title: '品类', dataIndex: 'category', key: 'category', width: 90 },
            { title: '单位', dataIndex: 'unit', width: 60 },
            { title: '数量', dataIndex: 'qty', width: 70,
              customRender: ({ text }: { text: number | null }) => text ?? '—' },
          ]"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'profession'">
              <span style="color:rgba(0,0,0,0.45);font-size:12px">{{ record.profession || '—' }}</span>
            </template>
            <template v-if="column.key === 'category'">
              <a-tag v-if="record.category" color="blue" style="margin:0">{{ record.category }}</a-tag>
              <a-tag v-else color="orange" style="margin:0">待确认</a-tag>
            </template>
          </template>
        </a-table>

        <!-- Force unknown category checkbox (shown only when unknowns exist) -->
        <div
          v-if="tenderPreview.unknown_count > 0"
          style="margin-top:10px;padding:8px 12px;background:#fff7e6;border:1px solid #ffa940;border-radius:4px;display:flex;align-items:center;gap:8px;font-size:13px"
        >
          <a-checkbox v-model:checked="forceUnknownCategory">
            强制归入默认品类「{{ tenderCategory || taskConfig.category }}」（{{ tenderPreview.unknown_count }} 项未识别将写入审计标记）
          </a-checkbox>
        </div>
      </div>
    </a-card>

    <!-- Step 2: Upload Supplier Quotes -->
    <a-card v-else-if="currentStep === 2" :body-style="{ padding: '20px' }">
      <!-- Context bar -->
      <div style="margin-bottom:16px;padding:10px 12px;background:#f6f8fa;border-radius:6px;display:flex;gap:10px;align-items:center;font-size:12px;color:rgba(0,0,0,0.55)">
        <FileExcelOutlined style="color:#52c41a" />
        <span>采购清单：<strong>{{ tenderFile?.name }}</strong>（{{ tenderPreview?.total ?? 0 }} 项）</span>
        <span v-if="tenderCategory" style="margin-left:4px">· 品类：<strong>{{ tenderCategory }}</strong></span>
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
              <a-button v-if="!f.confirmed" size="small" type="text" danger @click="removeBatchEntry(f)">移除</a-button>
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
                <a-select
                  v-if="f.matchedSupplierId"
                  v-model:value="f.matchedSupplierId"
                  style="width:200px"
                  size="small"
                  show-search
                  :filter-option="(input: string, opt: { label?: unknown }) => String(opt.label ?? '').includes(input)"
                >
                  <a-select-option v-for="s in allSuppliers" :key="s.id" :value="s.id" :label="s.name">{{ s.name }}</a-select-option>
                </a-select>
                <a-input
                  v-else
                  v-model:value="f.detectedSupplierName"
                  size="small"
                  style="width:200px"
                  placeholder="供应商名称"
                />
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
          :supplier-ids="effectiveSupplierIds.length ? effectiveSupplierIds : undefined"
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
            label="推荐主供"
            :value="matrixSummary.recommended_supplier?.name ?? '—'"
          />
          <StatCard
            :icon="DollarOutlined"
            icon-bg="rgba(250,140,22,0.1)"
            label="最优组合总价"
            :value="'¥' + matrixSummary.optimal_total.toLocaleString()"
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

      <!-- ③ Supplier evaluation cards (multi-supplier only) -->
      <div v-if="matrixSummary && matrixResult && !isSingleSupplierMode" class="supplier-eval">
        <h3 class="section-title">供应商综合评估</h3>
        <a-row :gutter="[14, 14]">
          <a-col
            v-for="s in matrixSuppliers"
            :key="s.id"
            :xs="24" :sm="12" :lg="6"
          >
            <div
              class="eval-card"
              :class="{
                'eval-card--recommended': matrixSummary.recommended_supplier?.id === s.id,
              }"
            >
              <div class="eval-card__header">
                <span class="eval-card__badge">{{ s.letter }}</span>
                <div class="eval-card__name-block">
                  <span class="eval-card__name">{{ s.name }}</span>
                  <a-tag
                    v-if="matrixSummary.recommended_supplier?.id === s.id"
                    color="blue"
                    style="margin-left:6px;font-size:10px"
                  >★ 推荐</a-tag>
                </div>
              </div>
              <div class="eval-card__metrics">
                <div class="eval-card__metric">
                  <span class="eval-card__metric-label">报价总额</span>
                  <span class="eval-card__metric-value">
                    ¥{{ (matrixTotals.find(t => t.supplier_id === s.id)?.total ?? 0).toLocaleString() }}
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
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.anomaly_count ?? 0) === 0" color="cyan">无异常</a-tag>
                <a-tag v-if="(matrixTotals.find(t => t.supplier_id === s.id)?.avg_deviation ?? 0) < 0" color="blue">价格优势</a-tag>
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
                <CheckCircleOutlined style="color:#52c41a" /> 推荐方案
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
            <span class="result-bottom-bar__total">
              推荐方案总价：<strong>¥{{ matrixSummary.optimal_total.toLocaleString() }}</strong>
            </span>
            <a-tag v-if="savingsPercent" color="green" style="margin-left:8px">
              节省 {{ savingsPercent }}%
            </a-tag>
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
