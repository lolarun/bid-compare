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
import { asQuoteShape, asQualityMeta, asDeclaredTotal } from '@/utils/extraction'
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

// design/29 §10 req3：卡片徽标的四个类别里，投标侧占两个——「投标文件」
// （整份 PDF 投标文件）与「报价清单」（Excel/CSV 报价明细表）。两者走的是
// 同一条上传/识别/入库管线（这里不分叉），差别只在卡片上怎么称呼它，所以
// 只是随行带一个标签，不是两套状态机。
export type BidDocKind = 'bid' | 'bid_list'

/** 扩展名兜底判定：分类接口没给结论（刷新恢复、手动卡片直传）时用它。
 *  只认扩展名这一个字面事实，不猜内容——猜错会让徽标撒谎。 */
export function inferBidDocKind(filename: string): BidDocKind {
  return /\.(xlsx?|csv)$/i.test(filename) ? 'bid_list' : 'bid'
}

export interface BatchFileEntry {
  id: string           // unique key
  filename: string
  // 卡片徽标用的文档类别（design/29 §10 req3）——优先取分类接口的判定，
  // 拿不到时按扩展名兜底。
  docKind: BidDocKind
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
  // design/24：文件名/OCR识别/当前输入名称三者不一致时的提示——纯展示，从不阻断
  // 入库（用户反馈 #6："能不能用个UI控件？或者说这个都不应该有提示"，决策是
  // "不再打断，卡片内联标注"）。空数组 = 三者一致，卡片不显示这块。
  nameConflictHints: string[]
  items: QuoteExtractionItem[]
  quality: QualityMeta | null   // 评审 R2：BLOCKED/REVIEW 横幅 + 台账，job.result._quality
  // design/29 §10 req5：文件自己声明的投标总价（job.result._doc_meta.bid_total）。
  // 跟明细逐行相加的合计分开存、分开显示——两者不一致正是要人工核对的信号。
  declaredTotal: number | null
  confirmedSupplierId: number | null    // null for unknown suppliers
  confirmedSubmissionId: number | null  // always set on confirm success
  confirmed: boolean
  confirming: boolean   // R1 止血：校对入库请求进行中——双击守卫，防止重复提交
  // 这份结果是**什么时候识别出来的**（job.created_at）。识别逻辑改动后旧结果
  // 不会自动更新——`create_job` 的幂等键 (file_hash, type, context_hash) 里
  // **没有代码版本**，重传同一份文件会命中旧 job 拿回旧结果。把识别时间摆在
  // 卡片上，用户才有依据判断"这个数是不是老的、要不要重新识别"。
  recognizedAt: string | null
  reRecognizing: boolean   // 强制重新识别请求进行中——双击守卫，同 confirming
  // design/44 §4.3：这张卡片被「更新报价文件」替换过几次（0 = 从未替换过，
  // 即最初上传的那份）。**不是**"重新识别"（同一份文件重跑，见 reRecognizing）
  // ——这是换了一份新文件。卡片据此显示"已更新·替换 N 次"而不是"识别于"，
  // 让用户分得清"重跑同一份"和"换了一份"这两件事。
  updateCount: number
  updating: boolean   // 更新报价文件请求进行中——双击守卫，同 confirming/reRecognizing
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
  // 桥接（2026-08-13）：missing_total 复核弹窗的"去核对这些行"按钮——调用方
  // 据此展开/滚动到对应文件的行级编辑器。不传时退化为纯提示，见
  // batchConfirmError.ts::handleBatchConfirmError 的 onViewDetails 参数。
  onMissingTotalDetails?: (fileId: string | number) => void
}) {
  const {
    taskConfig, tenderCategory, confirmedCategories, categoryExplicitlySelected, allSuppliers,
    onMissingTotalDetails,
  } = deps

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
      message.success(`已入库 ${result.line_count} 项报价${copyDedupNote(result.copy_dedup)}`)
    } catch (e) {
      // legacy 单供应商 tab 模式：ExtractionEditor 本来就常驻展示（不像批量卡片
      // 需要展开），missing_total 的"去核对"没有额外跳转目标，不传 onViewDetails。
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

  function handleBatchFile(file: File, docKind?: BidDocKind) {
    if (!file) return
    const duplicatePending = batchFiles.value.some(
      (entry) => entry.filename === file.name && !entry.confirmed,
    )
    if (duplicatePending) return

    const entry: BatchFileEntry = {
      id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      filename: file.name,
      docKind: docKind ?? inferBidDocKind(file.name),
      status: 'uploading',
      stage: '准备上传',
      stageDetail: '',
      progressPct: 1,
      uploadPct: 1,
      jobId: null,
      detectedSupplierName: '',
      // 上传当下就用文件名猜一个供应商名占位（可编辑，识别完成后如果拿到
      // 真实抽取名称会覆盖它）——不然用户要对着空输入框从零打字，还会撞见
      // a-auto-complete 拿全量 allSuppliers 当选项、输入几个字就弹一屏不相关
      // 候选的问题。识别结果回来时（下方 detectedSupplierName 赋值处）如果
      // 有真实供应商名，仍然按原逻辑覆盖这个猜测值。
      finalSupplierName: _extractSupplierHintFromFilename(file.name),
      matchedSupplierId: null,
      nameConflictHints: [],
      items: [],
      quality: null,
      declaredTotal: null,
      confirmedSupplierId: null,
      confirmedSubmissionId: null,
      confirmed: false,
      confirming: false, recognizedAt: null, reRecognizing: false,
      updateCount: 0, updating: false,
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

  /**
   * 连续查不到状态多久之后才放弃（毫秒）。
   *
   * design/29 §16：原来是"连续失败 5 次就判失败"，2 秒一次 → 10 秒就放弃。
   * 但**查不到状态不等于识别失败**——实测那次服务端每一次都返回了 200，是
   * 客户端 15s 超时先放弃的（多份扫描件同时识别时，识别线程占着 GIL 和
   * pdfium 锁，一次主键读也要排很久）。识别任务当时还在正常跑。
   *
   * 所以判据从"失败次数"改成"连续失败了多长时间"，并且给得很宽：查询本身
   * 是幂等的、几乎不花钱，多等一会儿的代价远小于把一个正在跑的任务标成失败
   * ——后者会让用户以为要重新上传，白花一次真实 OCR 的钱。
   */
  const POLL_GIVE_UP_MS = 3 * 60 * 1000

  function stopPolling(entry: BatchFileEntry) {
    if (entry.pollTimer) clearInterval(entry.pollTimer)
    entry.pollTimer = null
  }

  function startBatchPolling(entry: BatchFileEntry) {
    stopPolling(entry)
    let firstFailureAt = 0
    entry.pollTimer = setInterval(async () => {
      if (!entry.jobId) return
      try {
        const { data } = await intakeApi.getJob(entry.jobId)
        if (firstFailureAt) {
          // 恢复了就把话收回去，别让"连接不稳定"一直挂在卡片上。
          firstFailureAt = 0
          entry.stageDetail = ''
        }
        syncBatchProgress(entry, data)
        if (data.status === 'done') {
          stopPolling(entry)
          onBatchJobDone(entry, data)
        } else if (data.status === 'failed') {
          stopPolling(entry)
          entry.status = 'failed'
          entry.stage = '失败'
          entry.error = data.error || '识别失败'
        }
      } catch {
        const now = Date.now()
        if (!firstFailureAt) firstFailureAt = now
        const waited = now - firstFailureAt
        if (waited < POLL_GIVE_UP_MS) {
          // 还在忍耐期：如实说"连不上"，不说"失败"——两者对用户的含义
          // 完全不同（要不要重新上传）。
          entry.stageDetail = `连接不稳定，重试中（已 ${Math.round(waited / 1000)} 秒）`
          return
        }
        stopPolling(entry)
        entry.status = 'failed'
        entry.stage = '失败'
        entry.error = '连接中断，读不到识别状态（识别可能仍在后台进行，可点重试）'
      }
    }, 2000)
  }

  /**
   * 重试：**先重新挂上轮询，而不是重新识别**。
   *
   * 放弃轮询的常见原因是查询超时，这时后台那个 job 多半已经跑完了——重新
   * 上传会再花一次真实 OCR 的钱，还把已经算好的结果丢掉。只有 job 本身报
   * failed（服务端明确说失败）时重新上传才是对的，那条路径由用户重新拖文件
   * 走，这里不替他决定。
   */
  function retryBatchFile(entry: BatchFileEntry) {
    if (!entry.jobId) return
    entry.status = 'processing'
    entry.stage = '重新查询识别状态'
    entry.stageDetail = ''
    entry.error = ''
    startBatchPolling(entry)
  }

  function onBatchJobDone(entry: BatchFileEntry, job: ExtractionJob) {
    entry.status = 'done'
    entry.stage = '已识别'
    entry.progressPct = 100
    // 识别时间摆到卡片上（见 BatchFileEntry.recognizedAt 的说明）：识别逻辑
    // 改动后旧结果不会自动更新，用户需要一个依据判断这个数是不是老的。
    entry.recognizedAt = job.created_at || null
    const shape = asQuoteShape(job.result)
    entry.items = shape.items
    entry.quality = asQualityMeta(job.result)
    entry.declaredTotal = asDeclaredTotal(job.result)
    entry.detectedSupplierName = shape.supplier_name || ''
    // 品类兜底：招标文件没带采购清单时（实测有招标 PDF 写"详见附件1"而附件未
    // 装订），`tenderCategory` 恒为空，每一份报价都会被 batch-confirm 拒收，而
    // 界面上直到今天才有手动选择品类的入口。报价行自己投票出的品类（后端
    // 2026-08-23 新增，把握不足时为空串）在这里回填——**只在还没有品类时填**，
    // 绝不覆盖招标清单或用户手选的值：采购清单是更权威的来源。
    if (!tenderCategory.value && shape.detected_category) {
      tenderCategory.value = shape.detected_category
    }
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
    // 品类恢复：**必须在这里做**。下面重建卡片时，已入库的条目会被
    // `if (entry.confirmed) continue` 跳过、永远走不到 `onBatchJobDone`
    // （品类回填就挂在那个回调里），于是刷新一次品类就变回空串、点预览被
    // "还没有确定品类"挡住——而系统手里明明有品类（已确认的采购清单，或者
    // 已入库报价行上的 category）。后端 compare-state 现在一次性给出。
    if (!tenderCategory.value && data.category) {
      tenderCategory.value = data.category
    }
    const restored: BatchFileEntry[] = []
    for (const s of data.submissions) {
      restored.push({
        id: `restored-sub-${s.submission_id}`,
        filename: s.filename || `已入库报价 #${s.submission_id}`,
        // 刷新恢复拿不到当初的分类判定，按扩展名兜底（见 inferBidDocKind）。
        docKind: inferBidDocKind(s.filename || ''),
        status: 'done', stage: `已入库 ${s.line_count} 项`, stageDetail: '',
        progressPct: 100, uploadPct: 100,
        jobId: s.job_id,
        detectedSupplierName: s.supplier_raw_name,
        finalSupplierName: s.supplier_raw_name,
        matchedSupplierId: s.supplier_id,
        nameConflictHints: [],
        items: [],
        quality: null,
        declaredTotal: null,
        confirmedSupplierId: s.supplier_id,
        confirmedSubmissionId: s.submission_id,
        confirmed: true, confirming: false, recognizedAt: null, reRecognizing: false,
        updateCount: 0, updating: false, error: '', pollTimer: null,
      })
    }
    for (const j of data.inflight_jobs) {
      restored.push({
        id: `restored-job-${j.job_id}`,
        filename: j.filename || '报价文件',
        docKind: inferBidDocKind(j.filename || ''),
        status: j.status === 'failed' ? 'failed' : 'processing',
        stage: j.progress_stage || (j.status === 'done' ? '已识别' : '识别中'),
        stageDetail: formatStageDetail(j.stage_current, j.stage_total),
        progressPct: j.progress_pct || 0, uploadPct: 100,
        jobId: j.job_id,
        detectedSupplierName: '', finalSupplierName: '', matchedSupplierId: null,
        nameConflictHints: [],
        items: [],
        quality: null,
        declaredTotal: null,
        confirmedSupplierId: null, confirmedSubmissionId: null,
        confirmed: false, confirming: false, recognizedAt: null, reRecognizing: false,
        updateCount: 0, updating: false, error: '', pollTimer: null,
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
      // 2026-08-21 手测反馈修正：旧文案"请返回采购清单步骤"是 design/27
      // 退役旧向导之前的措辞——新工作台已经没有分步骤这回事了，指一个不
      // 存在的"步骤"只会让用户找不到路。改成直接说需要什么、上传区域就在
      // 同一屏（工作台顶部拖拽区/招标卡片），不引用任何具体页面结构。
      message.error(categories.length > 1
        ? '采购清单包含多个品类，请先选择本报价所属品类'
        : '还没有确定品类——请先上传/确认招标文件或采购清单，取得品类信息后再确认入库')
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

    // ── 三方名称提示：文件名提示 / OCR 识别 / 当前输入名称不一致时仅提示，不拦截 ──
    // design/24：用户反馈 #6 认为弹窗打断没有意义——当前输入的名称已经是用户
    // 编辑过的权威值，系统没有立场替用户判断"这就是有问题"。改成非阻断的卡片
    // 内联提示（entry.nameConflictHints），入库照常进行。
    entry.nameConflictHints = computeNameConflictHints(entry, supplierName)

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
      message.success(`${supplierName}${unknownNote}：已入库 ${data.line_count} 项报价${copyDedupNote(data.copy_dedup)}`)
    } catch (e: unknown) {
      // 注：后端 confirm_batch 从未产出过 "supplier_alias_conflict" 这个错误形状
      // （供应商同名合并走 /suppliers 的另一条独立解析路径），此前这里有一段处理
      // 它的 window.confirm 分支是永远走不到的死代码，一并清掉。
      if (await handleBatchConfirmError(
        e, message,
        () => onMissingTotalDetails?.(entry.id),
        (indexes) => {
          // 用户在弹窗里核对备注后确认"这些行原文确实未报价"——把标记写回
          // 对应的 item，下面的重试就会带着它一起提交。下标是后端按
          // `enumerate(overrides)` 给出的，跟 entry.items 数组位置直接对应。
          for (const i of indexes) {
            const it = entry.items[i]
            if (it) it.not_quoted = true
          }
        },
      )) {
        entry.confirming = false
        await confirmBatchEntry(entry, true)  // 用户核对差异/确认未报价后重新入库
      }
    } finally {
      entry.confirming = false
    }
  }

  /** 强制重新识别：拿服务端已存盘的原文件重跑，返回新 job，卡片就地接管。
   *
   *  **不是重新上传**——重传同一份文件会再次命中同一个幂等键
   *  `(file_hash, type, context_hash)`，拿回的还是旧结果。后端
   *  `/intake/jobs/{id}/re-recognize` 走 `force=True` 跳过幂等命中。
   *
   *  识别是要花钱的，所以这个动作只能由用户点，绝不自动触发。
   */
  async function reRecognizeEntry(entry: BatchFileEntry) {
    if (!entry.jobId || entry.reRecognizing) return
    entry.reRecognizing = true
    try {
      const { data } = await intakeApi.reRecognize(entry.jobId)
      entry.jobId = data.id
      entry.status = 'processing'
      entry.stage = '重新识别中'
      entry.progressPct = 0
      entry.error = ''
      // 旧的确认状态必须清掉——结果换了，之前基于旧结果的"已入库"不再成立。
      entry.confirmed = false
      entry.confirmedSubmissionId = null
      startBatchPolling(entry)
      message.success('已开始重新识别')
    } catch (e: unknown) {
      message.error(extractErrMsg(e, '重新识别失败'))
    } finally {
      entry.reRecognizing = false
    }
  }

  /** 更新本轮报价（design/44 §4.3）：换一份新文件，替换这张卡片当前的报价。
   *
   *  跟「重新识别」（reRecognizeEntry）不是一回事——那是**同一份文件**重跑；
   *  这是**新文件**。旧的已确认报价（若有）先 supersede（保留可复活），
   *  再走跟首次上传一样的识别管线，就地接管同一张卡片（不新开一张），
   *  供应商归属（finalSupplierName/matchedSupplierId）原样保留，不用户
   *  重新填一遍。结果留在"待确认"——跟 reRecognizeEntry 一致，识别和确认
   *  分两步，不擅自替用户点确认。
   */
  async function updateQuoteFile(entry: BatchFileEntry, file: File) {
    if (entry.updating || entry.reRecognizing) return
    entry.updating = true
    try {
      if (entry.confirmed && entry.confirmedSubmissionId != null) {
        await quoteApi.supersedeSubmission(entry.confirmedSubmissionId)
      }
    } catch (e: unknown) {
      message.error(extractErrMsg(e, '更新报价文件失败：无法替换旧版本'))
      entry.updating = false
      return
    }
    if (entry.pollTimer) { clearInterval(entry.pollTimer); entry.pollTimer = null }
    entry.filename = file.name
    entry.status = 'uploading'
    entry.stage = '准备上传'
    entry.stageDetail = ''
    entry.progressPct = 1
    entry.uploadPct = 1
    entry.jobId = null
    entry.items = []
    entry.quality = null
    entry.declaredTotal = null
    entry.confirmed = false
    entry.confirmedSupplierId = null
    entry.confirmedSubmissionId = null
    entry.recognizedAt = null
    entry.error = ''
    entry.updateCount += 1
    try {
      await uploadBatchFile(entry, file)
      message.success('已上传新文件，识别完成后请重新核对入库')
    } finally {
      entry.updating = false
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

  // 从文件名中提取供应商名称提示（用于冲突检测 + 卡片预填）
  // 旧命名规范「泰科龙投标文件.pdf」→「泰科龙」（供应商名在最前面）；
  // 2026-08-21 命名规范换成「项目名-供应商名+文档类型.ext」（项目名在最
  // 前面，供应商名在"-"之后）——例「徐汇区华泾镇项目-亨通投标文件.pdf」，
  // 原来的"取第一段"在新规范下会把项目名当成供应商名（三家供应商的卡片
  // 全显示同一个项目名，手测直接复现了这个问题）。先按字面"-"把项目名
  // 部分切掉，供应商名从"-"之后的部分里再按常见切割词提取；没有"-"时
  // （旧命名规范）整串都是供应商部分，行为不变，向后兼容。
  function _extractSupplierHintFromFilename(filename: string): string {
    const base = filename.replace(/\.(pdf|xlsx?|csv|docx?)$/i, '')
    const afterProjectPrefix = base.includes('-') ? base.split('-').slice(1).join('-') : base
    const parts = afterProjectPrefix.split(/[投标报价文件单_\s··【】()（）]+/)
    return (parts[0] || '').trim()
  }

  // design/24：文件名提示 / OCR 识别 / 当前输入名称三者不一致时的人话提示——
  // 纯展示用途，不做任何判断谁对谁错、不阻断任何操作。导出给 Stage 组件在用户
  // 编辑名称输入框时实时调用，让卡片内联提示随输入更新（而不是只在点「校对入库」
  // 那一刻才算一次）。
  function computeNameConflictHints(entry: BatchFileEntry, typedName?: string): string[] {
    const supplierName = (typedName ?? entry.finalSupplierName).trim()
    if (!supplierName) return []
    const filenameHint = _extractSupplierHintFromFilename(entry.filename)
    const ocrName = entry.detectedSupplierName
    const hints: string[] = []
    if (filenameHint && !supplierName.includes(filenameHint) && !filenameHint.includes(supplierName.slice(0, 4))) {
      hints.push(`文件名提示：「${filenameHint}」`)
    }
    if (ocrName && ocrName !== supplierName && !ocrName.includes(supplierName) && !supplierName.includes(ocrName.slice(0, 4))) {
      hints.push(`OCR 识别：「${ocrName}」`)
    }
    return hints
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
    retryBatchFile,
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
    reRecognizeEntry,
    updateQuoteFile,
    removeAllBatchEntries,
    restoreBatchFiles,
    clearAllBatchFiles,
    computeNameConflictHints,
  }
}
