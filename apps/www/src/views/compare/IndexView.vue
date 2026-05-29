<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckCircleOutlined, LineChartOutlined, RightOutlined, LeftOutlined,
  CloudUploadOutlined, LoadingOutlined, CheckOutlined, CloseCircleOutlined,
  PlusOutlined,
  AppstoreOutlined, TeamOutlined, TrophyOutlined, DollarOutlined,
  WarningOutlined, BulbOutlined, RobotOutlined,
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
  ImportResult,
  AlignmentGroup,
  AlignmentFieldFix,
  AlignmentSuggestResult,
  AlignmentApplyGroup,
  AlignmentApplyFieldFix,
} from '@/api/client'
import IntakeUploader from '@/components/IntakeUploader.vue'
import ExtractionEditor from '@/components/ExtractionEditor.vue'
import BrandTierModal from '@/components/BrandTierModal.vue'
import StatCard from '@/components/StatCard.vue'
import BidMatrix from './components/BidMatrix.vue'
import { normalizeAlert, formatDeviation } from '@/utils/alert'
import { asQuoteShape } from '@/utils/extraction'

const PROFESSION_CATEGORIES: Record<string, string[]> = {
  '电气': ['桥架', '母线槽', '配电箱'],
  '给排水': ['阀门', '不锈钢管', '水箱', '潜水泵'],
  '暖通': ['风口风阀', '风机盘管', '空调泵'],
}
const PROFESSIONS = Object.keys(PROFESSION_CATEGORIES)

// ─── State ───────────────────────────────────────────────────────────────
const currentStep = ref(0)
const selectedProfession = ref<string | undefined>(undefined)

const filteredCategories = computed(() => {
  if (!selectedProfession.value) return []
  return PROFESSION_CATEGORIES[selectedProfession.value] || []
})

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

// Bid matrix result for Step 3
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)

// Brand-tier modal
const brandModalVisible = ref(false)
const brandsToTier = ref<string[]>([])

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
const canProceedFromConfig = computed(
  () => !!taskConfig.category && (taskConfig.supplierIds.length >= 1 || taskConfig.supplierIds.length === 0)
)
// AUDIT-FIX C1: require explicit confirmation OR "skip with history" for every supplier.
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

// ─── AI Alignment (Step 2 for multi-supplier) ──────────────────────────
const alignmentResult = ref<AlignmentSuggestResult | null>(null)
const alignmentLoading = ref(false)
// Per-group status: confirmed / rejected (default: confirmed)
const alignmentGroupStatus = ref<Record<number, 'confirmed' | 'rejected'>>({})
// Per-fix status: accepted / rejected (default: accepted)
const alignmentFixStatus = ref<Record<number, boolean>>({})

// Whether alignment step applies (multi-supplier only)
const needsAlignmentStep = computed(() => effectiveSupplierIds.value.length >= 2)

// Effective step count based on mode
const STEP_RESULTS = computed(() => needsAlignmentStep.value ? 3 : 2)

async function runAlignmentSuggest() {
  alignmentLoading.value = true
  alignmentResult.value = null
  alignmentGroupStatus.value = {}
  alignmentFixStatus.value = {}
  try {
    const sids = effectiveSupplierIds.value
    // Mode 2: let backend query quote rows from DB
    const { data } = await analysisApi.alignmentSuggest({
      project_id: taskConfig.projectId,
      category: taskConfig.category,
      supplier_ids: sids,
      rows: [],
    })
    alignmentResult.value = data

    if (data.error) {
      message.error(`AI 分析失败：${data.error}`)
    } else {
      // Default: all groups confirmed, all fixes accepted
      data.groups.forEach((_: AlignmentGroup, i: number) => { alignmentGroupStatus.value[i] = 'confirmed' })
      data.field_fixes.forEach((_: AlignmentFieldFix, i: number) => { alignmentFixStatus.value[i] = true })
      const gn = data.groups.length
      const fn = data.field_fixes.length
      if (gn > 0 || fn > 0) {
        message.success(`AI 发现 ${gn} 组对齐建议、${fn} 个字段纠错`)
      } else {
        message.info('AI 未发现需要对齐的行，可直接进入比价')
      }
    }
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'AI 对齐分析失败'
    message.error(detail)
  } finally {
    alignmentLoading.value = false
  }
}

async function applyAlignmentAndRunMatrix() {
  // Apply confirmed alignment groups + accepted fixes
  if (alignmentResult.value) {
    const groups: AlignmentApplyGroup[] = []
    const fixes: AlignmentApplyFieldFix[] = []

    for (const [iStr, status] of Object.entries(alignmentGroupStatus.value)) {
      const i = Number(iStr)
      const g = alignmentResult.value.groups[i]
      if (!g) continue
      groups.push({
        suggested_name: g.suggested_name,
        suggested_spec: g.suggested_spec,
        confidence: g.confidence,
        reason: g.reason,
        status,
        items: g.items.map(it => ({
          quote_id: it.quote_id,
          supplier_id: it.supplier_id,
          action: it.action,
          spec_note: it.spec_note,
          name_note: it.name_note,
        })),
      })
    }

    for (const [iStr, accepted] of Object.entries(alignmentFixStatus.value)) {
      if (!accepted) continue
      const i = Number(iStr)
      const f = alignmentResult.value.field_fixes[i]
      if (!f) continue
      fixes.push({
        quote_id: f.quote_id,
        field: f.field,
        new_value: f.suggested,
      })
    }

    if (groups.length > 0 || fixes.length > 0) {
      try {
        await analysisApi.alignmentApply({
          project_id: taskConfig.projectId,
          category: taskConfig.category,
          groups,
          field_fixes: fixes,
        })
      } catch (e: unknown) {
        const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '保存对齐结果失败'
        message.error(detail)
      }
    }
  }
  runMatrix()
}

function skipAlignment() {
  currentStep.value = STEP_RESULTS.value
  runMatrix()
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
function goNext() {
  if (currentStep.value === 0) {
    if (!canProceedFromConfig.value) {
      message.warning('请选择品类（供应商可选）')
      return
    }
    currentStep.value = 1
  } else if (currentStep.value === 1) {
    if (!canProceedFromUpload.value) {
      message.warning('请为每家供应商点击「校对入库」或「使用历史数据」')
      return
    }
    if (needsAlignmentStep.value) {
      currentStep.value = 2
      runAlignmentSuggest()
    } else {
      // Single-supplier: skip alignment, go straight to results
      currentStep.value = STEP_RESULTS.value
      runMatrix()
    }
  } else if (currentStep.value === 2 && needsAlignmentStep.value) {
    // From alignment step → results
    currentStep.value = 3
    applyAlignmentAndRunMatrix()
  }
}

function goBack() {
  if (currentStep.value > 0) {
    // AUDIT-FIX M3: clear stale matrix when stepping back from results
    if (currentStep.value === STEP_RESULTS.value) {
      matrixResult.value = null
    }
    if (currentStep.value === 2 && needsAlignmentStep.value) {
      alignmentResult.value = null
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
    slot.unknown_brands = result.unknown_brands || []
    message.success(`已入库 ${result.created} 条报价`)
    if (slot.unknown_brands.length > 0) {
      brandsToTier.value = slot.unknown_brands
      brandModalVisible.value = true
    }
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
function isExcelFile(file: File) {
  return /\.(xlsx?|xls)$/i.test(file.name)
}

function handleBatchFile(file: File) {
  if (!file) return
  const duplicatePending = batchFiles.value.some(
    (entry) => entry.filename === file.name && !entry.confirmed,
  )
  if (duplicatePending) return

  if (isExcelFile(file)) {
    handleExcelBatchFile(file)
    return
  }

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

async function handleExcelBatchFile(file: File) {
  const entry: BatchFileEntry = {
    id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    filename: file.name,
    status: 'uploading',
    stage: '解析 Excel',
    progressPct: 30,
    uploadPct: 0,
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

  const form = new FormData()
  form.append('file', file)
  form.append('category', taskConfig.category)
  if (taskConfig.projectId) {
    form.append('project_id', String(taskConfig.projectId))
    const pName = projects.value.find((p) => p.id === taskConfig.projectId)?.name
    if (pName) form.append('project_name', pName)
  }
  if (taskConfig.bidStatus) {
    form.append('bid_status', taskConfig.bidStatus)
  }
  try {
    const { data } = await quoteApi.import(form)
    const result = data as ImportResult
    entry.status = 'done'
    entry.stage = '已导入'
    entry.progressPct = 100
    entry.confirmed = true

    if (result.supplier_ids?.length >= 1) {
      entry.confirmedSupplierId = result.supplier_ids[0]
      const sup = allSuppliers.value.find((s) => s.id === result.supplier_ids[0])
      entry.detectedSupplierName = sup?.name || ''
    }
    if (result.supplier_ids?.length > 1) {
      await fetchSuppliers()
      for (let i = 1; i < result.supplier_ids.length; i++) {
        const sid = result.supplier_ids[i]
        const sup = allSuppliers.value.find((s) => s.id === sid)
        batchFiles.value.push({
          id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          filename: `${file.name} (${sup?.name || `供应商${sid}`})`,
          status: 'done',
          stage: '已导入',
          progressPct: 100,
          uploadPct: 100,
          jobId: null,
          detectedSupplierName: sup?.name || '',
          matchedSupplierId: sid,
          items: [],
          confirmedSupplierId: sid,
          confirmed: true,
          error: '',
          pollTimer: null,
        })
      }
    }
    message.success(`Excel 导入完成：${result.imported} 条报价，${result.supplier_ids?.length || 0} 家供应商`)
  } catch (e) {
    entry.status = 'failed'
    entry.stage = '导入失败'
    const errResp = (e as any)?.response?.data?.detail
    if (typeof errResp === 'string') {
      entry.error = errResp
    } else if (errResp?.errors?.[0]?.reason) {
      entry.error = errResp.errors[0].reason
    } else {
      entry.error = (e as Error).message || '导入失败'
    }
  }
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
    if (data.unknown_brands?.length) {
      brandsToTier.value = data.unknown_brands
      brandModalVisible.value = true
    }
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '入库失败'
    message.error(detail)
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

// ─── Step 3: run bid-matrix ──────────────────────────────────────────────
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
      category: taskConfig.category || undefined,
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
      <a-step title="配置任务" description="项目 + 品类 + 供应商" />
      <a-step title="录入报价" description="按供应商上传或使用历史数据" />
      <a-step v-if="needsAlignmentStep" title="AI 对齐复核" description="智能匹配同一报价行" />
      <a-step title="比价结果" :description="isSingleSupplierMode ? '报价 vs 历史价格对比' : '横向矩阵 + 推荐供应商'" />
    </a-steps>

    <!-- Step 0: Configure -->
    <a-card v-if="currentStep === 0" :body-style="{ padding: '20px' }">
      <a-form layout="vertical">
        <a-form-item label="项目（可选；不选则跨项目比价）">
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

        <a-form-item label="专业" required>
          <a-select v-model:value="selectedProfession" placeholder="先选专业" style="width:180px">
            <a-select-option v-for="p in PROFESSIONS" :key="p" :value="p">{{ p }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="品类（必选）" required>
          <a-select
            v-model:value="taskConfig.category"
            placeholder="再选品类"
            style="width:280px"
            :disabled="!selectedProfession"
          >
            <a-select-option v-for="c in filteredCategories" :key="c" :value="c">{{ c }}</a-select-option>
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
            勾选后导入的报价不在热力图/气泡图中显示，但参与品牌档位比价与邀标推荐
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

    <!-- Step 1: Upload (batch mode OR per-supplier mode) -->
    <a-card v-else-if="currentStep === 1" :body-style="{ padding: '20px' }">

      <!-- Batch mode: no suppliers pre-selected -->
      <template v-if="useBatchMode">
        <a-upload-dragger
          :multiple="true"
          accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls"
          :show-upload-list="false"
          :before-upload="(file: File) => { handleBatchFile(file); return false; }"
        >
          <p class="ant-upload-drag-icon"><CloudUploadOutlined /></p>
          <p class="ant-upload-text">拖入所有供应商的报价文件</p>
          <p class="ant-upload-hint">支持 PDF / 图片（OCR 识别）和 Excel（直接解析）· 多文件同时上传</p>
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

    <!-- Step: AI Alignment Review (multi-supplier only) -->
    <a-card v-else-if="currentStep === 2 && needsAlignmentStep" :body-style="{ padding: '20px' }">
      <div v-if="alignmentLoading" style="text-align:center; padding:40px 0">
        <a-spin size="large" />
        <div style="margin-top:12px; color:#666">AI 正在分析报价对齐...</div>
        <div style="margin-top:4px; color:#999; font-size:12px">通常需要 30~60 秒</div>
      </div>
      <div v-else-if="alignmentResult">
        <!-- Error state -->
        <a-alert v-if="alignmentResult.error" type="warning" :message="alignmentResult.error" show-icon style="margin-bottom:16px" />

        <!-- Info bar -->
        <div style="margin-bottom:16px; display:flex; gap:16px; align-items:center; flex-wrap:wrap">
          <a-tag color="blue">{{ alignmentResult.groups.length }} 组对齐建议</a-tag>
          <a-tag color="orange">{{ alignmentResult.field_fixes.length }} 个字段纠错</a-tag>
          <span style="color:#999; font-size:12px">
            耗时 {{ (alignmentResult.duration_ms / 1000).toFixed(1) }}s
            / {{ alignmentResult.tokens_used }} tokens
          </span>
        </div>

        <!-- Alignment groups -->
        <div v-if="alignmentResult.groups.length > 0">
          <h4 style="margin:0 0 12px">对齐分组建议</h4>
          <a-collapse>
            <a-collapse-panel
              v-for="(group, gi) in alignmentResult.groups"
              :key="gi"
              :header="`${group.suggested_name} — ${group.suggested_spec}`"
            >
              <template #extra>
                <a-space @click.stop>
                  <a-tag :color="group.confidence >= 0.9 ? 'green' : group.confidence >= 0.7 ? 'orange' : 'red'">
                    {{ (group.confidence * 100).toFixed(0) }}%
                  </a-tag>
                  <a-switch
                    :checked="alignmentGroupStatus[gi] === 'confirmed'"
                    checked-children="确认"
                    un-checked-children="拒绝"
                    @change="(v: boolean) => alignmentGroupStatus[gi] = v ? 'confirmed' : 'rejected'"
                  />
                </a-space>
              </template>
              <p style="color:#666; margin-bottom:8px">{{ group.reason }}</p>
              <a-table
                :data-source="group.items"
                :columns="[
                  { title: 'Quote ID', dataIndex: 'quote_id', width: 100 },
                  { title: '供应商', dataIndex: 'supplier_id', width: 100 },
                  { title: '操作', dataIndex: 'action', width: 80 },
                  { title: '备注', dataIndex: 'spec_note' },
                ]"
                :pagination="false"
                size="small"
                :row-key="(r: Record<string, unknown>) => `${r.quote_id}`"
              />
            </a-collapse-panel>
          </a-collapse>
        </div>

        <!-- Field fixes -->
        <div v-if="alignmentResult.field_fixes.length > 0" style="margin-top:20px">
          <h4 style="margin:0 0 12px">字段纠错建议</h4>
          <a-table
            :data-source="alignmentResult.field_fixes"
            :columns="[
              { title: 'Quote ID', dataIndex: 'quote_id', width: 100 },
              { title: '字段', dataIndex: 'field', width: 100 },
              { title: '当前值', dataIndex: 'current', width: 120 },
              { title: '建议值', dataIndex: 'suggested', width: 120 },
              { title: '置信度', dataIndex: 'confidence', width: 100,
                customRender: ({ text }: { text: number }) => `${(text * 100).toFixed(0)}%` },
              { title: '原因', dataIndex: 'reason' },
              { title: '接受', key: 'accept', width: 80 },
            ]"
            :pagination="false"
            size="small"
            :row-key="(_r: Record<string, unknown>, i: number) => `fix-${i}`"
          >
            <template #bodyCell="{ column, record, index }">
              <template v-if="column.key === 'accept'">
                <a-checkbox
                  :checked="alignmentFixStatus[index] !== false"
                  @change="(e: { target: { checked: boolean } }) => alignmentFixStatus[index] = e.target.checked"
                />
              </template>
              <template v-else-if="column.dataIndex === 'current'">
                <span style="color:#999">{{ record.current }}</span>
              </template>
              <template v-else-if="column.dataIndex === 'suggested'">
                <span style="color:#1677ff; font-weight:500">{{ record.suggested }}</span>
              </template>
            </template>
          </a-table>
        </div>

        <!-- No suggestions -->
        <a-empty
          v-if="alignmentResult.groups.length === 0 && alignmentResult.field_fixes.length === 0 && !alignmentResult.error"
          description="AI 未发现需要对齐的报价行，可直接进入比价"
        />
      </div>
    </a-card>

    <!-- Step: Results -->
    <template v-else-if="currentStep === STEP_RESULTS">
      <!-- Context tags -->
      <div v-if="matrixSummary" class="result-context">
        <a-tag color="default">{{ taskConfig.category }}</a-tag>
        <a-tag v-if="selectedProjectName" color="default">{{ selectedProjectName }}</a-tag>
      </div>

      <!-- ① Summary stat cards -->
      <div v-if="matrixSummary" class="result-stats">
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

      <!-- ② Matrix table -->
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
            返回修改
          </a-button>
          <a-button @click="runMatrix">
            <template #icon><LineChartOutlined /></template>
            重新比价
          </a-button>
        </a-space>
      </div>
    </template>

    <!-- Footer nav (before results) -->
    <div v-if="currentStep < STEP_RESULTS" class="compare-page__footer">
      <a-button v-if="currentStep > 0" @click="goBack">
        <template #icon><LeftOutlined /></template>
        上一步
      </a-button>
      <template v-if="currentStep === 2 && needsAlignmentStep">
        <a-button @click="skipAlignment" :disabled="alignmentLoading">
          跳过对齐
          <template #icon><RightOutlined /></template>
        </a-button>
        <a-button type="primary" @click="goNext" :disabled="alignmentLoading">
          确认并比价
          <template #icon><RightOutlined /></template>
        </a-button>
      </template>
      <a-button v-else type="primary" @click="goNext">
        下一步
        <template #icon><RightOutlined /></template>
      </a-button>
    </div>

    <BrandTierModal
      v-model:visible="brandModalVisible"
      :brands="brandsToTier"
      :category="taskConfig.category"
    />

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
</style>
