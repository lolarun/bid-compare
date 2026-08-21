<!--
  design/27 §10 步骤3 —— 供应商主轴工作台骨架（header + materials strip +
  tabs + project bootstrap）。新路由，跟旧 5 步向导（IndexView.vue）并存，
  互不影响；退役旧向导 + 路由重定向是步骤5的范围，这里不动 IndexView.vue。

  三条约束（步骤2复核，2026-08-13）：
  1. 项目回填区分"没抽到"（留空+提示可填）vs"抽到空"（原文没有该字段，静态
     标注，不重复提醒）——见 fieldSourceLabel()。
  2. tab 徽标数字只读 useDoubtInbox，不另起计数。
  3. materials strip 的投标文件上传复用 useSupplierUpload，不重写。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import {
  CloudUploadOutlined, FilePdfOutlined, FileExcelOutlined, LoadingOutlined,
  HistoryOutlined, DownloadOutlined, SolutionOutlined, InboxOutlined,
} from '@ant-design/icons-vue'
import { projectApi, supplierApi, analysisApi, intakeApi } from '@/api'
import type { Supplier, TenderBidlistResult, BidMatrixResult, ExtractionJob } from '@/api/client'
import { useSupplierUpload } from '@/composables/useSupplierUpload'
import type { BatchFileEntry } from '@/composables/useSupplierUpload'
import { useDoubtInbox } from '@/composables/useDoubtInbox'
import QuoteGrid from '@/components/QuoteGrid.vue'
import BidMatrix from './components/BidMatrix.vue'
import IntakeUploader from '@/components/IntakeUploader.vue'
import type { QuoteGridColumn, DoubtMark } from '@/univer/quoteGridController'

const route = useRoute()
const router = useRouter()

// ─── 项目引导（feedback #1）：新建比价 → 空工作台 → 拖入第一份文档回填 ──────
const projectId = ref<number | null>(route.params.projectId ? Number(route.params.projectId) : null)
const projectName = ref('')
const projectCode = ref('')
const category = ref('')
const projectNameUserEdited = ref(false)   // 用户手动改过名称后，自动回填不再覆盖
const projectCodeUserEdited = ref(false)
const bootstrapping = ref(false)

async function ensureProject(): Promise<number> {
  if (projectId.value) return projectId.value
  bootstrapping.value = true
  try {
    // projects 表对 (name, code) 有唯一约束（业务规则：不允许字面重名项目）。
    // 用户还没输入名字时的占位名必须带上区分后缀——固定字面量"新比价项目"
    // 在多次新建空工作台后必然撞车（第二次点"新建"就 500）。这个占位名从不
    // 显示在输入框里（projectName 这个 ref 仍是空串，input 走 placeholder
    // 文案），后续自动回填或用户手动输入都会用 persistProjectMeta() 覆盖掉，
    // 所以后缀不会被用户看见。
    const placeholderName = projectName.value || `新比价项目-${Date.now()}`
    const { data } = await projectApi.create({
      name: placeholderName, code: projectCode.value, location: '', status: 'active', remark: '',
    })
    projectId.value = data.id
    router.replace(`/workspace/${data.id}`)
    return data.id
  } finally {
    bootstrapping.value = false
  }
}

async function persistProjectMeta() {
  if (!projectId.value) return
  await projectApi.update(projectId.value, { name: projectName.value, code: projectCode.value })
}

async function onProjectNameInput() {
  projectNameUserEdited.value = true
  await persistProjectMeta()
}
async function onProjectCodeInput() {
  projectCodeUserEdited.value = true
  await persistProjectMeta()
}

onMounted(async () => {
  if (projectId.value) {
    try {
      const { data } = await projectApi.get(projectId.value)
      projectName.value = data.name
      projectCode.value = data.code
    } catch { /* 项目不存在时留空，用户可重新新建 */ }
  } else {
    // 立即建项目（不等第一次上传才建）：URL 马上带上 projectId，刷新页面
    // 不丢工作台状态；上传组件（IntakeUploader）需要 project_id 才能把
    // context 传给后端，等到"选完文件才建项目"会让第一次上传多等一轮网络。
    await ensureProject()
  }
  // 后端 page_size 上限 100（Query(..., le=100)）；500 会直接 422，导致这个
  // "取全量供应商列表"的调用每次都失败，静默地把 allSuppliers 留空。跟
  // history/IndexView.vue 同一用途的调用保持一致改成 100。
  const { data: suppliers } = await supplierApi.list({ page: 1, page_size: 100 })
  allSuppliers.value = suppliers.items
})

// ─── 供应商列表（useSupplierUpload 需要） ─────────────────────────────────
const allSuppliers = ref<Supplier[]>([])

// ─── Step 2「投标文件」复用 useSupplierUpload（约束3：不重写） ─────────────
const taskConfig = reactive({
  projectId: undefined as number | undefined,
  category: '', supplierIds: [] as number[], bidStatus: '',
})
watch(projectId, (id) => { taskConfig.projectId = id ?? undefined }, { immediate: true })
watch(category, (c) => { taskConfig.category = c }, { immediate: true })

const {
  batchFiles, handleBatchFile, confirmBatchEntry, removeBatchEntry,
} = useSupplierUpload({
  taskConfig,
  tenderCategory: category,
  confirmedCategories: computed(() => (category.value ? [category.value] : [])),
  categoryExplicitlySelected: ref(true),
  allSuppliers,
  onMissingTotalDetails: (fileId) => { activeTab.value = String(fileId) },
})

async function onDropBidFiles(file: File) {
  await ensureProject()
  handleBatchFile(file)
}

// ─── 招标文件（materials strip）：复用 IntakeUploader（上传+轮询+进度+失败
//     重试都是它已经处理好的，不再手写第二份轮询逻辑——跟约束3"复用
//     useSupplierUpload、不重写"同一个精神，扩展到这个组件）。IntakeUploader
//     原本的文案只分 tender/其余两档，这里给 'tender_bidlist' 补了同款文案
//     （components/IntakeUploader.vue 的小扩展，不是另起一份组件）。────────
const tenderResult = ref<TenderBidlistResult | null>(null)
const tenderJob = ref<ExtractionJob | null>(null)
const tenderError = ref('')
// 2026-08-21 手测反馈：拖多个文件进来看不出总共传了几个——招标文件（一旦
// 开始上传/识别，tenderJob 就非空）+ 投标文件数量的总和，跟 classifyingCount
// 不是一回事（那个只反映"分类接口正在跑"这一瞬间，几秒就归零）。
const uploadedFileCount = computed(() => (tenderJob.value ? 1 : 0) + batchFiles.value.length)
const uploaderContext = computed(() => ({ project_id: projectId.value ?? undefined }))
// design/29 §3/§6：统一拖拽区是唯一入口，招标文件的实际上传/轮询逻辑还是
// IntakeUploader（约束3同一个精神：复用，不重写）——程序化触发它，不再手写
// 第二份上传逻辑。这个 ref 只在"自动路由到招标"和"轮询期间不想露出手动
// 卡片"这段窗口期为 true，跟 IntakeUploader 内部状态解耦，不依赖跨组件读
// exposed ref 的响应性细节。
const tenderUploaderRef = ref<InstanceType<typeof IntakeUploader> | null>(null)
const tenderAutoRouting = ref(false)

function onTenderExtracted(job: ExtractionJob) {
  tenderJob.value = job
  tenderError.value = ''
  tenderAutoRouting.value = false
  onTenderDone(job.result as unknown as TenderBidlistResult)
}
function onTenderFailed(err: string) {
  tenderError.value = err
  tenderAutoRouting.value = false
  // 自动路由触发的上传失败了——不能让用户对着一个隐藏的卡片手足无措，
  // 露出手动区域兜底（design/29 §6 的"不阻塞其余进度"原则同样适用于这里：
  // 自动化这条路走不通，人工路径必须还在）。
  showManualCards.value = true
}
function resetTender() {
  tenderJob.value = null
  tenderResult.value = null
  tenderError.value = ''
}

function onTenderDone(result: TenderBidlistResult) {
  tenderResult.value = result
  if (result.detected_category && !category.value) category.value = result.detected_category
  // 项目回填（约束1）：只在用户没手动改过时覆盖，覆盖的是"识别到的值"，
  // 抽不到时 project_name/project_code 是空字符串，不会覆盖成空——用户已
  // 输入的内容不会被清空。
  if (result.project_name && !projectNameUserEdited.value) projectName.value = result.project_name
  if (result.project_code && !projectCodeUserEdited.value) projectCode.value = result.project_code
  if (projectId.value) persistProjectMeta()
}

// design/27 §3.1 feedback #2：三产物各自独立呈现——封面/品牌要求的"有没有"
// 跟采购清单的"有没有"是三件独立的事，不能因为其中一个空就整体报"识别
// 结果为空"（那正是这轮要修的笼统态）。
const tenderCoverInfoPresent = computed(() =>
  !!(tenderResult.value?.project_name || tenderResult.value?.project_code || tenderResult.value?.deadline))
const tenderBrandInfoPresent = computed(() =>
  !!(tenderResult.value?.brand_requirement.length || tenderResult.value?.supplier_brands.length))

const excelFile = ref<File | null>(null)
const excelPreviewing = ref(false)
const excelError = ref('')
async function uploadExcel(file: File) {
  await ensureProject()
  excelPreviewing.value = true
  excelError.value = ''
  try {
    const form = new FormData()
    form.append('file', file)
    const { data } = await analysisApi.tenderListPreview(form)
    // 预览成功才落 excelFile——之前预览失败时也会把卡片标成"✓ 已上传"（只有
    // 一条转瞬即逝的 toast 报错，卡片本身撒谎说成功了），是"看起来没出错"
    // 这类静默缺口的一个真实实例，不是假设出来的。
    excelFile.value = file
    if (data.detected_category && !category.value) category.value = data.detected_category
    message.success(`采购清单已预览：${data.total} 条`)
  } catch (e: unknown) {
    excelError.value = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '预览失败'
    message.error(excelError.value)
  } finally {
    excelPreviewing.value = false
  }
}

// ─── design/28 cut 5 + design/29 §3：拖一堆文件进来自动分类 ────────────────
// Tier 0（瞬时、零模型调用，document_classify.py）对 Excel 是确定性判据
// ——无价格列→采购清单，价格列几乎填满→报价清单，两者之间→不确定。
// PDF 现在也有真实判据（design/29 Tier 1.5，scanned_pdf_classify.py）：
// 原生文字层走零模型调用的封面关键词判据；扫描件因为视觉判定实测 0/7
// 不可靠（design/29 §3.1），恒为 uncertain，不调用模型硬猜。
// 每一份的路由结果都用 message 提示出来（design/28 §5 red line 1"结果必须
// 可见"）；uncertain（Excel 判不出/PDF 扫描件/PDF 判据不够明确）一律弹窗
// 二选一（askTenderOrBid），不是让用户自己去猜该走哪个手动区域。分类接口
// 本身失败（网络/500）才露出手动卡片——不是"猜不出来就露卡片"，是"自动化
// 这条路彻底走不通才需要人工兜底"。design/29 §1：三张精确卡片不再是默认
// 可见的入口，但没删，见下方 showManualCards。
const classifyingCount = ref(0)
const showManualCards = ref(false)

// 真正把文件送进对应上传管线——招标走 IntakeUploader.handleFile（唯一持有
// 上传+轮询+失败重试逻辑的地方，不重写），投标/清单走已有的
// onDropBidFiles/uploadExcel（同样不重写）。
async function routeToTender(file: File) {
  tenderAutoRouting.value = true
  if (!tenderUploaderRef.value) {
    // IntakeUploader 只在 !tenderResult 时挂载（见模板）；正常情况下走到
    // 这里时它必然已挂载，这个分支是防御性的，不该在真实交互中触发。
    tenderAutoRouting.value = false
    showManualCards.value = true
    message.error(`「${file.name}」招标上传组件未就绪，请手动上传`)
    return
  }
  await tenderUploaderRef.value.handleFile(file)
}

async function routeToBid(file: File) {
  await onDropBidFiles(file)
}

// design/29 附加要求（2026-08-20）：PDF 判不出招标/投标时弹窗二选一，不再
// 是"提示一下、让用户自己去对应卡片重新拖"——那个台阶已经去掉了。
function askTenderOrBid(file: File, reasonHint: string) {
  Modal.confirm({
    title: `「${file.name}」看不出是招标文件还是投标文件`,
    content: reasonHint,
    okText: '招标文件',
    cancelText: '投标文件',
    onOk: () => routeToTender(file),
    onCancel: () => routeToBid(file),
  })
}

async function classifyAndRouteFile(file: File) {
  classifyingCount.value++
  try {
    const form = new FormData()
    form.append('file', file)
    let result
    try {
      const { data } = await intakeApi.classifyTier0(form)
      result = data
    } catch {
      message.error(`「${file.name}」分类接口异常，请直接使用下方区域手动上传`)
      showManualCards.value = true
      return
    }

    if (result.kind === 'excel') {
      if (result.verdict === 'tender_list') {
        message.info(`「${file.name}」识别为采购清单（无价格列），按此处理`)
        await uploadExcel(file)
      } else if (result.verdict === 'bid_list') {
        message.info(`「${file.name}」识别为报价清单（价格列填充率 ${((result.fill_rate ?? 0) * 100).toFixed(0)}%），按投标文件处理`)
        await onDropBidFiles(file)
      } else {
        askTenderOrBid(file, `Excel 是清单还是报价单不确定（${result.reason}），这份文件本身是招标方还是投标方提供的？`)
      }
      return
    }

    if (result.kind === 'pdf') {
      // design/29 §3 Tier 1.5：原生 PDF 走零模型调用的关键词判据，扫描件
      // 走视觉判定（2026-08-21 从"仅第一页缩略图"改成"前几页原生分辨率图
      // + 修正提示词"后，真实语料复测 0/7→8/8，接口现在两条路径都会给出
      // 真实判定，不再对扫描件恒答 uncertain）——两条路径给同一套 verdict
      // 语义，这里不用关心具体走的哪条，判不出来（真的 uncertain）时才弹窗。
      if (result.verdict === 'tender') {
        message.info(`「${file.name}」识别为招标文件（${result.reason}）`)
        await routeToTender(file)
      } else if (result.verdict === 'bid') {
        message.info(`「${file.name}」识别为投标文件（${result.reason}）`)
        await routeToBid(file)
      } else {
        askTenderOrBid(file, result.text_layer === 'scanned'
          ? `视觉判定信息不足以确定招投标类型（${result.reason || '未配置视觉客户端或识别信息不够'}），请人工确认。`
          : `文字层判据不够明确（${result.reason || '招投标关键词均未命中，或两侧都命中'}），请人工确认。`)
      }
      return
    }

    message.warning(`「${file.name}」${result.reason || '不支持的文件类型'}`)
  } finally {
    classifyingCount.value--
  }
}

async function onDropAnyFiles(file: File) {
  classifyAndRouteFile(file)
  return false
}

// ─── 疑点收件箱（约束2：tab 徽标只读这里，不另起计数） ─────────────────────
// design/27 §10 步骤4：对齐核查独立视图需要 category + submission_ids
// （AnchorReviewMatrix 的必需 props）——工作台已经知道这些值，通过 query
// 带过去，不用让用户在核查页里重新选一遍。
function goToAlignment() {
  if (!projectId.value) return
  router.push({
    path: `/workspace/${projectId.value}/align`,
    query: {
      category: category.value,
      submission_ids: confirmedSubmissionIds.value.join(',') || undefined,
    },
  })
}

const { dryRunByFile, dryRunLoading, refreshDryRun } = useDoubtInbox({
  batchFiles,
  taskConfig,
  reconcileResult: ref(null),
  reconcileConfirmed: ref(false),
  anchorReviewResult: ref(null),
  onGoToFile: (fileId: string) => { activeTab.value = fileId },
  onGoToReconcile: () => {},
  onGoToAlignment: goToAlignment,
})

function badgeCount(fileId: string): number {
  return dryRunByFile[fileId]?.issues?.length ?? 0
}

const dryRunAutoChecked = new Set<string>()
watch(batchFiles, (files) => {
  for (const f of files) {
    if (f.status === 'done' && !f.confirmed && f.finalSupplierName.trim() && !dryRunAutoChecked.has(f.id)) {
      dryRunAutoChecked.add(f.id)
      refreshDryRun(f)
    }
  }
}, { deep: true })

// ─── Tabs ──────────────────────────────────────────────────────────────
const activeTab = ref('list')
watch(batchFiles, (files) => {
  // 首次有报价文件时自动切到第一个 tab，体验上少一次点击；之后不再自动跳
  // （用户可能正在看别的 tab）。
  if (activeTab.value === 'list' && files.length === 1) activeTab.value = files[0].id
}, { deep: true })

// 手测反馈（2026-08-21）：明细表格（QuoteGrid，每个是一整个 Univer 引擎实例）
// 默认页面就卡顿——AntD a-tabs 默认全量挂载所有 pane，4 家供应商 = 4 个并发
// Univer 实例。改成"概述先行、明细是下一步"：默认只显示卡片，点卡片才进
// detail 模式显示 tabs+QuoteGrid+确认入库；即使进了 detail 模式，QuoteGrid
// 本身也只挂载 activeTab 对应的那一个（见模板里 QuoteGrid 外层的 v-if），
// 切 tab 时旧的先卸载——两层防护，不是只解决"默认页面卡"这一半。
const viewMode = ref<'overview' | 'detail'>('overview')
function openDetail(tabKey: string) {
  activeTab.value = tabKey
  viewMode.value = 'detail'
}

const gridColumns: QuoteGridColumn[] = [
  { key: 'material', title: '材料/设备名称' },
  { key: 'spec', title: '规格型号' },
  { key: 'unit', title: '计量单位' },
  { key: 'qty', title: '数量' },
  { key: 'unit_price', title: '单价' },
  { key: 'total_price', title: '合价' },
  { key: 'brand', title: '品牌' },
  { key: 'remark', title: '备注' },
]

// design/27 §10 步骤4 —— dry-run 行索引 → Univer 网格行号的映射（复核意见
// 明确点名的"关键接缝"，逐字写清楚，不能假设两者相等）：
//
// 后端 issue/warning 里的 `index` 是**提交给 batch-confirm 的 items 数组里
// 的 0-based 位置**（quote_confirmation_service.py 的 `for idx, item in
// enumerate(items)`），不是识别产物自己的 document_row_index——如果这份
// 文档识别时有副本被去重过滤掉，items 数组从一开始就已经是去重后的顺序，
// index 天然对齐的是"去重之后"的位置，不需要额外处理副本这一层。
//
// Univer 网格里，第 0 行是表头（quoteGridController.ts::toGrid 的
// `[header, ...body]`），所以数据第 k 行（0-based）落在网格第 k+1 行。
//
// 这个映射成立的前提是：dry-run 请求提交时的 items 数组，跟当前 QuoteGrid
// 绑定的 f.items **顺序和行数一致**——useDoubtInbox.refreshDryRun 提交的
// overrides 就是 f.items 本身（同一个引用），QuoteGrid 目前只支持逐格编辑
// 不支持增删行，所以顺序/行数在两次调用之间不会变，映射不会跑偏。若未来
// QuoteGrid 支持增删行，这个前提要重新核实，不能想当然继续 +1。
const GRID_ROW_OFFSET = 1

// 判据 flags → 表格标色 severity。BLOCKING 级问题（阻断入库）统一标红——
// 不管具体是哪种 flag，红色传达的是"现在就要处理"，跟 REVIEW 级的
// 黄/橙区分开；REVIEW 级按 flag 类型分色（design/27 §4 的三色判据）。
// duplicate_row 目前没有专属颜色（§4 只定义了三种），落黄——"值得看但没那么
// 紧急"，比强行发明第四种颜色更简单，等产品明确要单独视觉区分再加。
function severityForFlags(flags: string[], blocking: boolean): DoubtMark['severity'] {
  if (blocking) return 'missing'
  if (flags.includes('value_truncated')) return 'truncation'
  return 'arithmetic'   // arithmetic_mismatch / duplicate_row 都落这里
}

function doubtMarksFor(fileId: string): DoubtMark[] {
  const dr = dryRunByFile[fileId]
  if (!dr) return []
  const marks: DoubtMark[] = []

  for (const issue of dr.issues ?? []) {
    if (issue.error === 'missing_total_requires_review') {
      const rows = (issue.review_rows as Array<{ index: number; reason?: string; derived_total_candidate?: number }> | undefined) ?? []
      for (const r of rows) {
        marks.push({
          row: r.index + GRID_ROW_OFFSET, columnKey: 'total_price', severity: 'missing',
          hoverText: r.reason || '原文无合价，请核对原文后补写',
        })
      }
    } else if (issue.error === 'structural_integrity_requires_review') {
      const rows = (issue.review_rows as Array<{ index: number; flags: string[]; reason: string; column: string }> | undefined) ?? []
      for (const r of rows) {
        marks.push({
          row: r.index + GRID_ROW_OFFSET, columnKey: r.column, severity: severityForFlags(r.flags, true),
          hoverText: r.reason,
        })
      }
    }
  }

  // integrity.warnings：非阻断的 REVIEW 级疑点（重复/算术/截断），入库门不拦
  // 但表格里仍然要看得见——跟 blocking 的 review_rows 同一份 index 语义。
  for (const w of dr.integrity?.warnings ?? []) {
    marks.push({
      row: w.index + GRID_ROW_OFFSET, columnKey: w.column, severity: severityForFlags(w.flags, false),
      hoverText: w.reason,
    })
  }

  return marks
}

// ─── design/29 §4/§5：概述卡片 + 统计 ───────────────────────────────────────
// 概述是"锦上添花"，不是阻断项——拿不到（未配置文本客户端/网络异常）时卡片
// 照常可用，只是没有那一两句话，不影响任何数据流转（跟 design/27 红线1
// "系统只陈述已知事实"一致：拿不到就不陈述，不是拿假数据凑一句）。
const tenderSummary = ref('')
const tenderSummaryLoading = ref(false)
const supplierSummaries = reactive<Record<string, string>>({})
const supplierSummaryLoading = reactive<Record<string, boolean>>({})

watch(tenderResult, async (result) => {
  if (!result) { tenderSummary.value = ''; return }
  tenderSummaryLoading.value = true
  try {
    const { data } = await intakeApi.summarizeFacts('tender', {
      project_name: result.project_name || projectName.value,
      category: category.value,
      row_count: result.row_count,
      deadline: result.deadline,
    })
    tenderSummary.value = data.summary
  } catch {
    tenderSummary.value = ''
  } finally {
    tenderSummaryLoading.value = false
  }
})

async function fetchSupplierSummary(f: BatchFileEntry) {
  if (supplierSummaries[f.id] !== undefined || supplierSummaryLoading[f.id]) return
  supplierSummaryLoading[f.id] = true
  try {
    const { data } = await intakeApi.summarizeFacts('bid', {
      supplier_name: f.finalSupplierName || f.detectedSupplierName,
      row_count: f.items.length,
      category: category.value,
    })
    supplierSummaries[f.id] = data.summary
  } catch {
    // 静默失败——概述拿不到不阻断任何操作，见上方注释。
  } finally {
    supplierSummaryLoading[f.id] = false
  }
}

watch(batchFiles, (files) => {
  for (const f of files) {
    if (f.status === 'done') fetchSupplierSummary(f)
  }
}, { deep: true })

// design/29 §5 D3：报价总计口径——全部行都算（含待确认），标注待确认行数，
// 不是官方评估总价（那是比价矩阵/评估服务算的，两边不共用一套算法，避免
// "同一个业务结果两处算出两个数"——CLAUDE.md"必须消费同一个业务服务结果"）。
function bidStatsFor(f: BatchFileEntry) {
  const total = f.items.reduce((sum, item) => {
    const v = Number((item as unknown as Record<string, unknown>).total_price)
    return sum + (Number.isFinite(v) ? v : 0)
  }, 0)
  const pendingRows = new Set(doubtMarksFor(f.id).map((m) => m.row))
  return { count: f.items.length, total, pendingCount: pendingRows.size }
}

// ─── Step 4「结果」：复用 BidMatrix.vue ────────────────────────────────────
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)
const confirmedSubmissionIds = computed(() =>
  batchFiles.value.filter((f) => f.confirmed && f.confirmedSubmissionId != null).map((f) => f.confirmedSubmissionId!))

async function runAnalysis() {
  if (!projectId.value || confirmedSubmissionIds.value.length === 0) {
    message.warning('请至少完成一家供应商的「校对入库」再开始比价')
    return
  }
  analyzing.value = true
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: projectId.value, supplier_ids: [], submission_ids: confirmedSubmissionIds.value, category: category.value,
    })
    matrixResult.value = data
  } catch (e: unknown) {
    message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '比价分析失败')
  } finally {
    analyzing.value = false
  }
}

const matrixSuppliers = computed(() => matrixResult.value?.suppliers ?? [])
</script>

<template>
  <div class="workspace-view">
    <!-- Header：项目名/编号（回填+可编辑）+ 品类 + 供应商数 + 操作按钮 -->
    <div class="workspace-header">
      <div class="workspace-header__title">
        <a-input
          v-model:value="projectName"
          placeholder="比价项目名称（拖入招标文件后自动识别，可编辑）"
          :bordered="false"
          class="workspace-header__name-input"
          @change="onProjectNameInput"
        />
        <div class="workspace-header__meta">
          <a-input
            v-model:value="projectCode"
            placeholder="编号未识别，点击填写"
            :bordered="false"
            size="small"
            style="width:160px"
            @change="onProjectCodeInput"
          />
          <span v-if="category">· {{ category }}</span>
          <span v-if="batchFiles.length">· {{ batchFiles.length }} 家投标</span>
        </div>
      </div>
      <div class="workspace-header__actions">
        <a-button @click="goToAlignment">
          <SolutionOutlined />对齐核查
        </a-button>
        <a-button><HistoryOutlined />历史</a-button>
        <a-button type="primary"><DownloadOutlined />导出</a-button>
      </div>
    </div>

    <!-- design/28 cut 5 + design/29 §1/§3：拖一堆文件进来自动分类，是唯一
         上传入口——Excel 是确定性判据（无价格列→采购清单，填满→报价清单），
         PDF 原生文字层有真实判据可直接路由，扫描件/判不出来的一律弹窗二选一
         （不再是"提示一下让你自己去对应卡片重新拖"）。 -->
    <a-upload-dragger
      :show-upload-list="false" :multiple="true"
      accept=".pdf,.xlsx,.xls,.png,.jpg,.jpeg"
      :before-upload="onDropAnyFiles"
      class="auto-classify-dragger"
    >
      <p class="ant-upload-drag-icon"><InboxOutlined style="font-size:32px;color:#1677ff" /></p>
      <p class="ant-upload-text" style="font-size:14px">拖入招标文件（PDF）、投标文件（PDF）或采购清单（Excel）、报价清单（Excel）</p>
      <p class="ant-upload-hint" style="font-size:12px">
        <LoadingOutlined v-if="classifyingCount > 0" spin /> {{ classifyingCount > 0 ? '识别中…' : '自动识别归类；判不出来时会弹窗让你确认一下' }}
      </p>
      <!-- 手测反馈（2026-08-21）：拖多个文件进来时看不出总共传了几个——
           classifyingCount 只反映"正在跑分类接口"这一瞬间，文件一多这个数
           很快归零，不能当"已上传"总数看。总数改成 uploadedFileCount。 -->
      <p v-if="uploadedFileCount > 0" class="ant-upload-hint" style="font-size:12px;margin-top:4px">
        已上传 {{ uploadedFileCount }} 个文件
      </p>
    </a-upload-dragger>

    <!-- Materials strip：design/29 §1/§6——不再是默认可见的上传入口，只在
         有真实内容/自动路由失败兜底时才显示（showManualCards/tenderResult/
         tenderAutoRouting/excelFile 任一为真）。IntakeUploader 本身依然
         挂载在这里（不是删掉），供上方统一拖拽区通过 ref 程序化调用。 -->
    <div class="materials-strip">
      <div class="material-card" v-show="showManualCards || tenderResult || tenderAutoRouting">
        <template v-if="!tenderResult">
          <IntakeUploader
            ref="tenderUploaderRef"
            type="tender_bidlist"
            :context="uploaderContext"
            hint="自动识别采购清单、封面信息与品牌要求，通常 20-90 秒完成"
            @extracted="onTenderExtracted"
            @failed="onTenderFailed"
          />
        </template>
        <template v-else>
          <div class="material-card__header">
            <FilePdfOutlined style="color:#cf1322" />
            <span>招标文件</span>
            <a-button size="small" type="text" @click="resetTender">重新上传</a-button>
          </div>
          <!-- design/27 §3.1 feedback #2：三产物各自独立呈现，不用"识别结果为空"
               这种笼统态盖过去——采购清单/封面信息/品牌要求各有各的有无，互不
               代表彼此。 -->
          <div class="tender-artifacts">
            <div class="tender-artifacts__item" :class="{ 'tender-artifacts__item--empty': tenderResult.row_count === 0 }">
              <span class="tender-artifacts__label">采购清单</span>
              <span v-if="tenderResult.row_count > 0">✓ {{ tenderResult.row_count }} 项</span>
              <span v-else>正文无清单，请上传 Excel 附件 →</span>
            </div>
            <div class="tender-artifacts__item" :class="{ 'tender-artifacts__item--empty': !tenderCoverInfoPresent }">
              <span class="tender-artifacts__label">封面信息</span>
              <span v-if="tenderCoverInfoPresent">
                ✓
                <template v-if="tenderResult.project_name">项目名</template>
                <template v-if="tenderResult.project_code"> · 编号</template>
                <template v-if="tenderResult.deadline"> · 截止时间</template>
                已识别
              </span>
              <span v-else>封面未识别到项目名/编号/日期，可手动填写</span>
            </div>
            <div class="tender-artifacts__item" :class="{ 'tender-artifacts__item--empty': !tenderBrandInfoPresent }">
              <span class="tender-artifacts__label">品牌要求</span>
              <span v-if="tenderBrandInfoPresent">✓ {{ tenderResult.brand_requirement.length }} 项业主品牌 · {{ tenderResult.supplier_brands.length }} 家参与品牌</span>
              <span v-else>未识别到品牌要求（可能原文本就没有）</span>
            </div>
          </div>
        </template>
        <a-alert v-if="tenderError" type="error" :message="tenderError" show-icon banner style="margin-top:8px;padding:4px 8px" />
      </div>

      <div class="material-card"
           v-show="showManualCards || excelFile || excelPreviewing || (tenderResult && tenderResult.row_count === 0)"
           :class="{ 'material-card--highlight': tenderResult && tenderResult.row_count === 0 && !excelFile }">
        <template v-if="excelFile">
          <div class="material-card__header">
            <FileExcelOutlined style="color:#52c41a" />
            <span>采购清单 Excel ✓</span>
            <a-button size="small" type="text" @click="() => { excelFile = null }">重新上传</a-button>
          </div>
        </template>
        <a-upload-dragger v-else :show-upload-list="false" accept=".xlsx,.xls" :disabled="excelPreviewing"
          :before-upload="(f: File) => { uploadExcel(f); return false }" class="material-card__dragger">
          <p class="ant-upload-drag-icon"><FileExcelOutlined style="color:#52c41a;font-size:28px" /></p>
          <p class="ant-upload-text" style="font-size:13px">
            <LoadingOutlined v-if="excelPreviewing" spin /> {{ excelPreviewing ? '解析中…' : '上传采购清单 Excel' }}
          </p>
          <p class="ant-upload-hint" style="font-size:12px">正文没有清单表时的对照来源，可选</p>
          <a-alert v-if="excelError" type="error" :message="excelError" show-icon banner style="margin-top:8px;padding:4px 8px;text-align:left" />
        </a-upload-dragger>
      </div>

      <div class="material-card" v-show="showManualCards">
        <a-upload-dragger :show-upload-list="false" :multiple="true" accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls,.csv"
          :before-upload="(f: File) => { onDropBidFiles(f); return false }" class="material-card__dragger">
          <p class="ant-upload-drag-icon"><CloudUploadOutlined style="font-size:28px" /></p>
          <p class="ant-upload-text" style="font-size:13px">拖入所有投标文件</p>
          <p class="ant-upload-hint" style="font-size:12px">PDF / 图片 / Excel，可多选</p>
        </a-upload-dragger>
      </div>
    </div>

    <div v-if="!showManualCards && !tenderResult && !excelFile && !excelPreviewing" class="materials-strip__manual-toggle">
      <a-button size="small" type="link" @click="showManualCards = true">看不到卡片？点这里手动选择上传区域</a-button>
    </div>

    <!-- design/29 §2/§4/§5，手测反馈修正（2026-08-21）：概述卡片是默认唯一
         视图——招投标概述 + 清单/报价情况，纵向 100% 宽（不横排）。明细表格
         （含"确认入库"）是点卡片之后的下一步，不在这一屏——见下方
         viewMode==='detail' 那块。 -->
    <div v-if="tenderResult || batchFiles.length > 0" class="summary-cards">
      <div v-if="tenderResult" class="summary-card summary-card--tender" @click="openDetail('list')">
        <div class="summary-card__badge summary-card__badge--tender">招标</div>
        <div class="summary-card__body">
          <a-spin :spinning="tenderSummaryLoading" size="small">
            <div class="summary-card__text">{{ tenderSummary || (tenderSummaryLoading ? '' : (tenderResult.project_name || '招标文件')) }}</div>
          </a-spin>
          <div class="summary-card__stats">采购清单 {{ tenderResult.row_count }} 项</div>
        </div>
      </div>

      <div v-for="f in batchFiles" :key="f.id" class="summary-card summary-card--bid" @click="openDetail(f.id)">
        <div class="summary-card__badge summary-card__badge--bid">投标</div>
        <div class="summary-card__body">
          <template v-if="f.status === 'done'">
            <a-spin :spinning="!!supplierSummaryLoading[f.id]" size="small">
              <div class="summary-card__text">{{ supplierSummaries[f.id] || (f.finalSupplierName || f.filename) }}</div>
            </a-spin>
            <div class="summary-card__stats">
              报价清单 {{ bidStatsFor(f).count }} 行 · 报价总计 ¥{{ bidStatsFor(f).total.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }}
              <span v-if="bidStatsFor(f).pendingCount > 0" class="summary-card__stats-pending">
                （含 {{ bidStatsFor(f).pendingCount }} 行待确认，未计入官方评估）
              </span>
            </div>
          </template>
          <template v-else>
            <!-- 手测反馈（2026-08-21）：卡片已经确定是"投标"（上面 badge），
                 这里不该再让用户猜"到底分析出来没有"——直接给真实进度条 +
                 阶段文案（f.stage，比如"识别内容"/"整理完成"），不是裸的
                 百分比数字。 -->
            <div class="summary-card__text">{{ f.finalSupplierName || f.filename }}</div>
            <template v-if="f.status === 'failed'">
              <div class="summary-card__stats" style="color:#ff4d4f">识别失败：{{ f.error || '未知错误' }}</div>
            </template>
            <template v-else>
              <a-progress :percent="f.progressPct" size="small" status="active" />
              <div class="summary-card__stats">
                {{ f.stage || '分析中' }}{{ f.stageDetail ? `（${f.stageDetail}）` : '' }}
              </div>
            </template>
          </template>
        </div>
      </div>
    </div>

    <!-- 明细 + 确认入库——下一步，默认不显示（点卡片才进来，见 openDetail）。 -->
    <template v-if="viewMode === 'detail'">
      <a-button size="small" class="detail-back" @click="viewMode = 'overview'">← 返回概述</a-button>
      <a-tabs v-model:active-key="activeTab" class="workspace-tabs">
        <a-tab-pane key="list">
          <template #tab>清单{{ tenderResult ? ` · ${tenderResult.row_count}` : '' }}</template>
          <div style="padding:12px;color:rgba(0,0,0,0.45);font-size:13px">
            采购清单只读（对齐核查页处理逐行裁决，清单本身如有误请「重新上传」）——
            步骤4 接入完整清单表格视图。
          </div>
        </a-tab-pane>
        <a-tab-pane v-for="f in batchFiles" :key="f.id">
          <template #tab>
            {{ f.finalSupplierName || f.filename }}
            <a-badge v-if="badgeCount(f.id) > 0" :count="badgeCount(f.id)" :offset="[4, -2]" />
          </template>
          <div class="supplier-tab-content">
            <div v-if="f.status !== 'done' || f.confirmed === false" class="supplier-tab-content__progress">
              <template v-if="f.status === 'uploading' || f.status === 'processing'">
                <LoadingOutlined spin /> {{ f.stage }}<span v-if="f.stageDetail">（{{ f.stageDetail }}）</span> · {{ f.progressPct }}%
              </template>
              <template v-else-if="f.status === 'failed'">
                <span style="color:#ff4d4f">{{ f.error || '识别失败' }}</span>
              </template>
            </div>
            <template v-if="f.status === 'done'">
              <div class="supplier-tab-content__meta">
                <a-auto-complete
                  v-model:value="f.finalSupplierName"
                  placeholder="供应商名称"
                  :options="allSuppliers.map(s => ({ value: s.name, label: s.name, id: s.id }))"
                  @select="(_v: string, opt: { id?: number }) => { f.matchedSupplierId = opt.id ?? null }"
                  style="width:220px"
                  autocomplete="off"
                />
                <a-button :loading="dryRunLoading[f.id]" @click="refreshDryRun(f)">重新核对</a-button>
                <a-button type="primary" :loading="f.confirming" @click="confirmBatchEntry(f)">确认入库</a-button>
                <a-button danger @click="removeBatchEntry(f)">移除</a-button>
              </div>
              <!-- 只挂载当前激活 tab 的 QuoteGrid（每个都是一整个 Univer 引擎
                   实例）——AntD a-tabs 默认全量挂载所有 pane，不加这层 v-if
                   会导致 N 家供应商 = N 个并发 Univer 实例，这正是页面卡顿
                   的根因（2026-08-21 手测反馈）。 -->
              <QuoteGrid
                v-if="activeTab === f.id"
                :model-value="f.items as unknown as Record<string, unknown>[]"
                :columns="gridColumns"
                :doubt-marks="doubtMarksFor(f.id)"
                @update:model-value="(v) => { f.items = v as any }"
              />
            </template>
          </div>
        </a-tab-pane>
      </a-tabs>
    </template>

    <!-- Result section -->
    <div class="result-section">
      <a-button type="primary" :loading="analyzing" @click="runAnalysis">
        {{ matrixResult ? '重新分析' : '开始比价分析' }}
      </a-button>
      <BidMatrix
        v-if="matrixResult"
        :suppliers="matrixSuppliers"
        :rows="matrixResult.rows"
        :totals="matrixResult.totals"
        :category="category"
        :project-id="projectId ?? undefined"
        style="margin-top:16px"
      />
    </div>
  </div>
</template>

<style scoped>
.workspace-view { padding: 16px 24px; max-width: 1400px; margin: 0 auto; }

/* Header */
.workspace-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid #f0f0f0; }
.workspace-header__title { flex: 1; min-width: 0; }
.workspace-header__name-input { font-size: 20px; font-weight: 600; padding: 0; }
/* 空态占位符跟已填内容视觉上要分得开——不然读起来像"这一行本来就该是灰的"，
   而不是"这是一个等你填的提示"。placeholder 单独调浅、加斜体，输入框本身
   聚焦时才显示底边框，平时跟纯文本一样不突兀。 */
.workspace-header__name-input :deep(.ant-input)::placeholder { color: rgba(0,0,0,0.3); font-style: italic; font-weight: 400; }
.workspace-header__meta { display: flex; align-items: center; gap: 6px; font-size: 13px; color: rgba(0,0,0,0.45); margin-top: 4px; }
.workspace-header__meta :deep(.ant-input)::placeholder { color: rgba(0,0,0,0.3); font-style: italic; }
.workspace-header__actions { display: flex; gap: 8px; flex-shrink: 0; }

/* design/28 cut 5 自动分类拖拽区——视觉上比下方三张精确卡片更突出（更高、
   更亮的强调色），暗示"这是首选入口，下面三张是需要手动指定类型时的备选"。 */
.auto-classify-dragger { margin-bottom: 12px; border-color: #91caff; background: #f0f7ff; }
.auto-classify-dragger :deep(.ant-upload-drag-icon) { margin-bottom: 6px; }

/* Materials strip：三张等宽卡片，不是三个内联按钮——每张卡片自己决定内容
   （上传态用 dragger，完成态用摘要），卡片本身的边框/圆角/内边距统一。 */
.materials-strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
.materials-strip__manual-toggle { text-align: center; margin: -12px 0 20px; }

/* 2026-08-21 手测反馈：卡片改纵向、100% 宽，不再横排（原来 flex-wrap 横排
   在窄屏/多供应商时挤成一团，也不是"先看概述"该有的阅读顺序）。 */
.summary-cards { display: flex; flex-direction: column; gap: 12px; margin-bottom: 20px; }
.summary-card {
  width: 100%;
  border: 1px solid #e8e8e8; border-radius: 8px; padding: 12px;
  background: #fff; cursor: pointer; transition: box-shadow 0.15s, border-color 0.15s;
  display: flex; gap: 10px;
}
.summary-card:hover { border-color: #1677ff; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.summary-card__badge {
  flex-shrink: 0; align-self: flex-start;
  font-size: 12px; font-weight: 500; padding: 2px 8px; border-radius: 4px;
}
.summary-card__badge--tender { background: #fff1f0; color: #cf1322; }
.summary-card__badge--bid { background: #e6f4ff; color: #1677ff; }
.summary-card__body { flex: 1; min-width: 0; }
.summary-card__text {
  font-size: 13px; color: rgba(0,0,0,0.85); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.summary-card__stats { font-size: 12px; color: rgba(0,0,0,0.45); margin-top: 6px; }
.summary-card__stats-pending { color: #d46b08; }
.material-card { border: 1px solid #e8e8e8; border-radius: 8px; padding: 12px; background: #fff; min-height: 96px; }
.material-card--highlight { border-color: #52c41a; box-shadow: 0 0 0 1px #52c41a; }
.material-card__header { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; margin-bottom: 8px; }
.material-card__dragger { padding: 4px 0; }
.material-card__dragger :deep(.ant-upload-drag-icon) { margin-bottom: 4px; }
.material-card :deep(.intake-uploader__dragger) { padding: 4px 0; }
.tender-artifacts { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: rgba(0,0,0,0.65); }
.tender-artifacts__item { display: flex; align-items: center; gap: 6px; }
.tender-artifacts__item--empty { color: rgba(0,0,0,0.4); }
.tender-artifacts__label { flex-shrink: 0; min-width: 56px; color: rgba(0,0,0,0.45); }

.detail-back { margin-bottom: 12px; }
.workspace-tabs { background: #fff; }
.supplier-tab-content { padding: 8px 0; }
.supplier-tab-content__progress { padding: 24px; text-align: center; color: rgba(0,0,0,0.55); }
.supplier-tab-content__meta { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.result-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid #f0f0f0; }
</style>
