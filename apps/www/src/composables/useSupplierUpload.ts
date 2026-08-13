/**
 * R5（低垂果实轮，先做低垂果实那块——完整拆分缓行）：从 compare/IndexView.vue
 * 抽出 Step 2「供应商报价上传」这一段（legacy 单供应商 tab 槽位 +
 * 新版批量上传两套状态、轮询、校对入库、移除、跨项目清空、刷新恢复）。
 *
 * 这一段是文件里少数几处真正自成一体的部分：对外只读 taskConfig / tenderCategory
 * / confirmedCategories / categoryExplicitlySelected / allSuppliers，只写
 * supplierUploads / batchFiles 自己的状态，不反向依赖招标清单对账（reconcile）
 * 或比价矩阵那两大块——那两块評审时读下来互相缠得很深，这轮不动，只挑这块。
 *
 * 迁移是纯搬运，不改行为：函数体、注释、双击守卫、失败计数上限等全部原样保留。
 */
import { ref, reactive, computed, watch, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import { quoteApi, intakeApi, analysisApi } from '@/api'
import type {
  ExtractionJob,
  QuoteExtractionItem,
  QualityMeta,
  BatchConfirmResult,
  CopyDedupInfo,
  Supplier,
} from '@/api/client'
import { asQuoteShape, asQualityMeta } from '@/utils/extraction'
import { handleBatchConfirmError } from '@/utils/batchConfirmError'
import { extractErrMsg } from '@/utils/errors'

// design/24 B2：阶段内进度的人话摘要——stage_total 为空 = 只有单调递增计数
// （逐页识别这个长阶段没有总数可言，见 dashscope_ocr.py::_mm_stream），两个
// 都有值才是真正的"第 N/共 M"（渲染页面）。用户反馈 #4 的症结正是"进度长期
// 停在逐页识别 20% 不动"——这里让它至少能看见数字在跳。
function formatStageDetail(current?: number | null, total?: number | null): string {
  if (current == null) return ''
  if (total == null) return `已转录 ${current} 行`
  return `${current}/${total} 页`
}

export interface UploadTaskConfig {
  projectId: number | undefined
  category: string
  supplierIds: number[]
  bidStatus: string
}

export interface BatchFileEntry {
  id: string           // unique key
  filename: string
  status: 'uploading' | 'processing' | 'done' | 'failed'
  stage: string
  // design/24 B2：阶段内进度的人话摘要（"已转录 320 行" / "3/8 页"），跟 stage
  // 分开存——stage 是步骤条按子串匹配当前步骤用的稳定标识，不能被拼接文字污染。
  stageDetail: string
  progressPct: number
  uploadPct: number
  jobId: string | null
  detectedSupplierName: string   // OCR-detected name (read-only source of truth)
  finalSupplierName: string      // user-editable display name (always takes precedence)
  matchedSupplierId: number | null  // set when user selects from dropdown; null = stranger
  items: QuoteExtractionItem[]
  quality: QualityMeta | null   // 评审 R2：BLOCKED/REVIEW 横幅 + 台账，job.result._quality
  confirmedSupplierId: number | null    // null for unknown suppliers
  confirmedSubmissionId: number | null  // always set on confirm success
  confirmed: boolean
  confirming: boolean   // R1 止血：校对入库请求进行中——双击守卫，防止重复提交
  error: string
  pollTimer: ReturnType<typeof setInterval> | null
}

export const BATCH_PROGRESS_STEPS = [
  { key: 'upload', label: '上传', pct: 1 },
  { key: 'received', label: '已接收', pct: 5 },
  { key: 'render', label: '渲染 PDF', pct: 10 },
  { key: 'split', label: '拆分页面', pct: 15 },
  { key: 'recognize', label: '逐页识别', pct: 20 },
  { key: 'merge', label: '合并结果', pct: 88 },
  { key: 'cleanup', label: '整理结果', pct: 95 },
  { key: 'done', label: '已识别', pct: 100 },
] as const

// design/24 B0：入库成功但识别到多份合法副本时，附一句人话说明——不加的话
// 用户会盯着 line_count 比预期少一半却不知道为什么（浦东 272 行只入了 136，
// 不说明就是新的"死胡同"，正是这轮改造要消灭的那种体验）。
function copyDedupNote(dedup: CopyDedupInfo | null | undefined): string {
  if (!dedup) return ''
  return `（识别到 ${dedup.total_copies} 份重复副本，已选第 ${dedup.selected_copy_no} 份入库，其余 ${dedup.dropped_rows} 行作证据留存）`
}

export function useSupplierUpload(deps: {
  taskConfig: UploadTaskConfig
  tenderCategory: Ref<string>
  confirmedCategories: Ref<string[]>
  categoryExplicitlySelected: Ref<boolean>
  allSuppliers: Ref<Supplier[]>
}) {
  const { taskConfig, tenderCategory, confirmedCategories, categoryExplicitlySelected, allSuppliers } = deps

  // Per-supplier upload state for Step 2 (legacy slot mode)
  const supplierUploads = reactive<Record<number, {
    job: ExtractionJob | null
    items: QuoteExtractionItem[]
    confirmed: boolean
    batch_id?: string
    unknown_brands: string[]
  }>>({})

  // ─── Batch upload state (new flow) ─────────────────────────────────────
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

  const canProceedFromUpload = computed(() => {
    if (useBatchMode.value) {
      return batchFiles.value.filter((f) => f.confirmed).length >= 1
    }
    return taskConfig.supplierIds.every((sid) => supplierUploads[sid]?.confirmed === true)
  })

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

  // 评审 R2（第3块）：batch-confirm 的两个结构化错误（checksum_ack / missing_total
  // review_rows）此前只走 extractErrMsg 落进裸 toast，见 handleBatchConfirmError
  // 的实现注释（utils/batchConfirmError.ts）——两个调用点（confirmSupplier /
  // confirmBatchEntry）和 import/IndexView.vue 共用同一份文案，不各写一份。

  // 评审 R2：legacy 模式（单供应商 tab）的质量分层横幅数据源——slot.job 已存了完整
  // ExtractionJob，直接从 job.result._quality 取，不需要像批量模式那样额外存字段。
  function slotQuality(supplierId: number): QualityMeta | null {
    return asQualityMeta(supplierUploads[supplierId]?.job?.result ?? null)
  }

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

  // R1 止血：双击守卫（同 confirmBatchEntry）。legacy 单供应商 tab 模式当前
  // 没有任何 UI 会把 taskConfig.supplierIds 填非空，这个函数因此实际不可达，
  // 但保留防护，避免以后重新接入时又是一次裸调用。
  const confirmingSuppliers = ref<Record<number, boolean>>({})
  async function confirmSupplier(supplierId: number, checksumAck = false) {
    const slot = supplierUploads[supplierId]
    if (!slot || !slot.job) {
      message.warning('请先上传该供应商的报价单')
      return
    }
    if (confirmingSuppliers.value[supplierId]) return
    const effectiveCategory = tenderCategory.value || taskConfig.category
    if (!effectiveCategory) {
      message.error('品类不能为空：请先完成招标清单识别后再入库')
      return
    }
    confirmingSuppliers.value[supplierId] = true
    try {
      const { data } = await quoteApi.batchConfirm({
        job_id: slot.job.id,
        supplier_id: supplierId,
        project_id: taskConfig.projectId,
        category: effectiveCategory,
        overrides: slot.items as unknown as Array<Record<string, unknown>>,
        bid_status: taskConfig.bidStatus,
        checksum_ack: checksumAck || undefined,
      })
      const result = data as BatchConfirmResult
      slot.confirmed = true
      slot.batch_id = result.batch_id
      message.success(`已入库 ${result.line_count} 条报价${copyDedupNote(result.copy_dedup)}`)
    } catch (e) {
      if (await handleBatchConfirmError(e, message)) {
        confirmingSuppliers.value[supplierId] = false  // 重试前先解锁，避免被自己的守卫挡住
        await confirmSupplier(supplierId, true)
      }
    } finally {
      confirmingSuppliers.value[supplierId] = false
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
      stageDetail: '',
      progressPct: 1,
      uploadPct: 1,
      jobId: null,
      detectedSupplierName: '',
      finalSupplierName: '',
      matchedSupplierId: null,
      items: [],
      quality: null,
      confirmedSupplierId: null,
      confirmedSubmissionId: null,
      confirmed: false,
      confirming: false,
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
      entry.stageDetail = formatStageDetail(job.stage_current, job.stage_total)
      entry.progressPct = job.progress_pct || 0
    } else if (job.status === 'running') {
      entry.status = 'processing'
      entry.stage = job.progress_stage || '识别中'
      entry.stageDetail = formatStageDetail(job.stage_current, job.stage_total)
      entry.progressPct = job.progress_pct || 10
    } else if (job.status === 'done') {
      entry.status = 'done'
      entry.stage = '已识别'
      entry.stageDetail = ''
      entry.progressPct = 100
    } else if (job.status === 'failed') {
      entry.status = 'failed'
      entry.stage = job.progress_stage || '失败'
      entry.stageDetail = ''
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
    entry.quality = asQualityMeta(job.result)
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
        status: 'done', stage: `已入库 ${s.line_count} 条`, stageDetail: '',
        progressPct: 100, uploadPct: 100,
        jobId: s.job_id,
        detectedSupplierName: s.supplier_raw_name,
        finalSupplierName: s.supplier_raw_name,
        matchedSupplierId: s.supplier_id,
        items: [],
        quality: null,
        confirmedSupplierId: s.supplier_id,
        confirmedSubmissionId: s.submission_id,
        confirmed: true, confirming: false, error: '', pollTimer: null,
      })
    }
    for (const j of data.inflight_jobs) {
      restored.push({
        id: `restored-job-${j.job_id}`,
        filename: j.filename || '报价文件',
        status: j.status === 'failed' ? 'failed' : 'processing',
        stage: j.progress_stage || (j.status === 'done' ? '已识别' : '识别中'),
        stageDetail: formatStageDetail(j.stage_current, j.stage_total),
        progressPct: j.progress_pct || 0, uploadPct: 100,
        jobId: j.job_id,
        detectedSupplierName: '', finalSupplierName: '', matchedSupplierId: null,
        items: [],
        quality: null,
        confirmedSupplierId: null, confirmedSubmissionId: null,
        confirmed: false, confirming: false, error: '', pollTimer: null,
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

  async function confirmBatchEntry(entry: BatchFileEntry, checksumAck = false) {
    // R1 止血：双击守卫。之前按钮没有任何 loading/disabled 绑定，网络慢时连
    // 点两下会打两次 batch-confirm（重复入库，靠后端幂等兜底而不是前端不发）。
    if (!entry.jobId || entry.confirming) return

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

    entry.confirming = true
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
        checksum_ack: checksumAck || undefined,
      })
      entry.confirmed = true
      entry.confirmedSupplierId = data.supplier_id ?? null
      entry.confirmedSubmissionId = data.submission_id ?? null
      const unknownNote = supplierId ? '' : '（陌生供应商，仅用于本次比价）'
      message.success(`${supplierName}${unknownNote}：已入库 ${data.line_count} 条报价${copyDedupNote(data.copy_dedup)}`)
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
          entry.confirming = false  // 重试前先解锁，否则 confirmBatchEntry 递归调用会被自己的守卫挡住
          await confirmBatchEntry(entry)  // 用确认的 supplier_id 重试
        } else {
          message.warning(`请在「供应商」下拉里手动选择正确的供应商后再入库`)
        }
      } else if (await handleBatchConfirmError(e, message)) {
        entry.confirming = false
        await confirmBatchEntry(entry, true)  // 用户核对差异后确认强制入库
      }
    } finally {
      entry.confirming = false
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

  // R1 止血：停掉所有在途轮询并清空报价文件卡片（批量模式 + legacy 单供应商
  // tab 两套状态都要清）。之前只在 onBeforeUnmount 用过一次——切项目时完全没
  // 调用，导致旧项目的 batchFiles（含仍在跑的 pollTimer）原样留在页面上，
  // 跟着新 project_id 一起提交，造成跨项目报价串号。
  function clearAllBatchFiles() {
    for (const f of batchFiles.value) {
      if (f.pollTimer) clearInterval(f.pollTimer)
    }
    batchFiles.value = []
    for (const key of Object.keys(supplierUploads)) {
      delete supplierUploads[Number(key)]
    }
  }

  return {
    supplierUploads,
    batchFiles,
    useBatchMode,
    batchProgress,
    canProceedFromUpload,
    effectiveSubmissionIds,
    effectiveSupplierIds,
    isSingleSupplierMode,
    confirmingSuppliers,
    slotQuality,
    onExtracted,
    confirmSupplier,
    skipSupplier,
    handleBatchFile,
    currentBatchStepIndex,
    batchStepState,
    confirmBatchEntry,
    removeBatchEntry,
    removeAllBatchEntries,
    restoreBatchFiles,
    clearAllBatchFiles,
  }
}
