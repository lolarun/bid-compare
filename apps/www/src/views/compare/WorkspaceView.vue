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
import { Modal, message } from 'ant-design-vue'
import {
  LoadingOutlined, HistoryOutlined, DownloadOutlined, SolutionOutlined, InboxOutlined,
} from '@ant-design/icons-vue'
import { projectApi, supplierApi, analysisApi, intakeApi } from '@/api'
import type { Supplier, TenderBidlistResult, BidMatrixResult, ExtractionJob } from '@/api/client'
import { useSupplierUpload, inferBidDocKind } from '@/composables/useSupplierUpload'
import type { BatchFileEntry, BidDocKind } from '@/composables/useSupplierUpload'
import { useDoubtInbox } from '@/composables/useDoubtInbox'
import QuoteGrid from '@/components/QuoteGrid.vue'
import BidMatrix from './components/BidMatrix.vue'
import IntakeUploader from '@/components/IntakeUploader.vue'
import type { QuoteGridColumn, DoubtMark } from '@/univer/quoteGridController'
import {
  CARD_KIND_LABEL, buildPendingCards, buildTenderCard, buildTenderListCard, buildBidCard,
  formatMoney,
} from '@/utils/docCards'
import type { DocCard, PendingClassifyCard } from '@/utils/docCards'
import type { BidMatrixPreviewResult } from '@/api/client'

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

// 保存失败的原因，常驻显示在名称输入框下方（不是一闪而过的 toast）：保存
// 失败时输入框里仍是用户/识别填进去的值，看起来跟保存成功没有区别，刷新才
// 发现名字没存上。这条提示是"没存上"的唯一可见证据，保存成功时清空。
const metaSaveError = ref('')

async function persistProjectMeta(opts: { auto?: boolean } = {}) {
  if (!projectId.value) return
  try {
    await projectApi.update(projectId.value, { name: projectName.value, code: projectCode.value })
    metaSaveError.value = ''
  } catch (e) {
    const res = (e as { response?: { status?: number; data?: { detail?: string } } })?.response
    if (res?.status === 409) {
      // 后端 uq_project_name_code：同名同编号的项目已存在。
      metaSaveError.value = res.data?.detail || '已有同名项目，请改个名称或编号'
      // 自动回填撞名基本只有一种真实含义：**这份招标文件之前已经比过一次**。
      // 名字是系统识别出来的、不是用户起的，让他去改一个自己没起过的名字来
      // 解决冲突，是把系统的问题推给他。所以先去把那个已有项目找出来，把
      // "打开它"作为首选出路；手动逐字输入不弹窗（会边打字边弹）。
      if (opts.auto) await offerExistingProject()
    } else {
      metaSaveError.value = '项目名称/编号未保存成功，请重试'
      if (opts.auto) message.error(metaSaveError.value)
    }
  }
}

/** 撞名时找出那个已有项目，问用户要不要直接打开它。 */
async function offerExistingProject() {
  let existing: { id: number; name: string } | null = null
  try {
    const { data } = await projectApi.findExact(projectName.value, projectCode.value)
    existing = data
  } catch { /* 查不到就退回下面那条纯提示，不因为附加查询失败而丢掉主线信息 */ }

  if (!existing) {
    message.error(`识别到的项目名「${projectName.value}」与已有项目重名，未保存：请手动改名或填不同编号`)
    return
  }
  // 用 confirm 而不是直接跳转：跳走会丢掉这个工作台里已经上传/识别的文件，
  // 那是用户刚花了几十秒等出来的东西，不能替他决定。
  Modal.confirm({
    title: '这份招标文件已经比过一次',
    content: `已有项目 #${existing.id}「${existing.name}」用的就是这个名称和编号。`
      + '打开它可以接着上次的结果；留在当前工作台的话，请改个名称或编号再保存。',
    okText: `打开 #${existing.id}`,
    cancelText: '留在当前工作台',
    onOk: () => { router.push(`/workspace/${existing!.id}`) },
  })
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
    // 打开已有项目：先问后端有没有已确认比价基准，否则按钮会对着一个
    // 早就确认过的项目继续显示"确认为比价基准"。
    refreshBaselineState()
  }
  // 2026-08-21：**打开页面时不再建项目**。原来这里无条件 ensureProject()，
  // 理由是"URL 马上带上 projectId，刷新不丢状态"——但空工作台刷新本来就没有
  // 状态可丢，代价却是**每打开一次页面就往 projects 表塞一行**。实测库里
  // 攒了 23 个零 BidSubmission 的「新比价项目-<时间戳>」空壳。
  // 四条真正需要 project_id 的路径（招标/采购清单/投标/报价清单）各自
  // await ensureProject()，改成懒建之后覆盖不变。
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
  batchFiles, retryBatchFile, handleBatchFile, confirmBatchEntry, removeBatchEntry,
} = useSupplierUpload({
  taskConfig,
  tenderCategory: category,
  confirmedCategories: computed(() => (category.value ? [category.value] : [])),
  categoryExplicitlySelected: ref(true),
  allSuppliers,
  onMissingTotalDetails: (fileId) => { activeTab.value = String(fileId) },
})

// ─── 招标文件（materials strip）：复用 IntakeUploader（上传+轮询+进度+失败
//     重试都是它已经处理好的，不再手写第二份轮询逻辑——跟约束3"复用
//     useSupplierUpload、不重写"同一个精神，扩展到这个组件）。IntakeUploader
//     原本的文案只分 tender/其余两档，这里给 'tender_bidlist' 补了同款文案
//     （components/IntakeUploader.vue 的小扩展，不是另起一份组件）。────────
const tenderResult = ref<TenderBidlistResult | null>(null)
const tenderJob = ref<ExtractionJob | null>(null)
const tenderError = ref('')
// 2026-08-21 手测反馈：拖多个文件进来看不出总共传了几个。design/29 §10 req1
// 把口径统一成"下面这些卡片一共几张"——待分类 + 招标 + 采购清单 + 投标/报价
// 清单，跟卡片区一一对应，不再是一个跟界面对不上的独立计数。
const uploadedFileCount = computed(
  () => pendingClassify.value.length
    + ((tenderJob.value || tenderAutoRouting.value) ? 1 : 0)
    + ((excelFile.value || excelPreviewing.value) ? 1 : 0)
    + batchFiles.value.length)
// design/29 §10 req1/req2：招标文件那张卡片也要有文件名和真实进度。
// IntakeUploader 内部当然知道这两件事，但它自己那套进度块已经撤掉（§12），
// 进度条——卡片区拿不到，就只能显示一张没有进度的空卡片。`@progress` 是它
// 早就有的出口（emit('progress', job)），这里接上，不重写第二份轮询。
const tenderFilename = ref('')
// design/29 §13：分类判据原文按文件名存一份，卡片徽标悬停时显示。判据不能
// 删——它是"系统凭什么这么判"的唯一说明（design/27 红线1）——但也不该整段
// 塞进 toast，实测是一屏读不完的文字。
const classifyReasons = reactive<Record<string, string>>({})
const tenderProgressPct = ref(0)
const tenderStage = ref('')
function onTenderProgress(job: ExtractionJob) {
  tenderProgressPct.value = job.progress_pct || 0
  tenderStage.value = job.progress_stage || '识别中'
}
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
  tenderProgressPct.value = 100
  tenderStage.value = ''
  if (!tenderFilename.value) tenderFilename.value = job.filename || ''
  onTenderDone(job.result as unknown as TenderBidlistResult)
}
function onTenderFailed(err: string) {
  // 招标卡片自己会显示 errorText（docCards 读 tenderError），加上这张卡片上
  // 的"重新上传"，用户有明确的下一步——不再需要额外露出一片手动上传区域
  // （design/29 §12：那片区域已撤，它是同一件事的第二个入口）。
  tenderError.value = err
  tenderAutoRouting.value = false
  tenderProgressPct.value = 0
  tenderStage.value = ''
}

function onTenderDone(result: TenderBidlistResult) {
  tenderResult.value = result
  if (result.detected_category && !category.value) category.value = result.detected_category
  // 项目回填（约束1）：只在用户没手动改过时覆盖，覆盖的是"识别到的值"，
  // 抽不到时 project_name/project_code 是空字符串，不会覆盖成空——用户已
  // 输入的内容不会被清空。
  if (result.project_name && !projectNameUserEdited.value) projectName.value = result.project_name
  if (result.project_code && !projectCodeUserEdited.value) projectCode.value = result.project_code
  if (projectId.value) persistProjectMeta({ auto: true })
  refreshBaselineState()
}

const excelFile = ref<File | null>(null)
const excelPreviewing = ref(false)
const excelError = ref('')
// design/29 §10 req1/req3/req6：采购清单 Excel 此前完全没有卡片——它既不是
// tenderResult 也不进 batchFiles，用户拖进来之后除了一条 toast 什么都看不到。
// 卡片要显示文件名和"多少项"，所以这两个值得留下来，不能只活在 toast 里。
const excelFilename = ref('')
const excelRowCount = ref<number | null>(null)
async function uploadExcel(file: File) {
  excelFilename.value = file.name
  excelPreviewing.value = true
  excelError.value = ''
  await ensureProject()
  try {
    const form = new FormData()
    form.append('file', file)
    const { data } = await analysisApi.tenderListPreview(form)
    // 预览成功才落 excelFile——之前预览失败时也会把卡片标成"✓ 已上传"（只有
    // 一条转瞬即逝的 toast 报错，卡片本身撒谎说成功了），是"看起来没出错"
    // 这类静默缺口的一个真实实例，不是假设出来的。
    excelFile.value = file
    excelRowCount.value = data.total
    if (data.detected_category && !category.value) category.value = data.detected_category
    message.success(`采购清单已预览：${data.total} 项`)
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
// 可见的入口（design/29 §12 起整体撤掉，只留统一拖拽区）。
// design/29 §10 req1/req2：拖进来的每一份文件**立刻**有一张卡片，类别未
// 判出时徽标就是「分析中」。此前只有一个 classifyingCount 计数：分类接口
// 几秒就返回，计数很快归零，而真正耗时的识别在别处——用户拖 5 个文件下去
// 页面上一张卡片都没有，只有一行"已上传 N 个文件"。卡片在这里建立、在路由
// 到具体管线时移交（dropPending），全程不留空窗。
// 卡片上那行状态文案（"判定文件类型…"/"等待确认类型"）随 note 走，见 docCards.ts
const pendingClassify = ref<PendingClassifyCard[]>([])

function addPending(file: File): PendingClassifyCard {
  const card: PendingClassifyCard = {
    id: `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    filename: file.name, note: '判定文件类型…', error: '',
  }
  pendingClassify.value.push(card)
  return card
}
// 一句话的完成提示 + 判据存进 classifyReasons（卡片徽标悬停可看）。
function noteClassified(filename: string, label: string, reason: string) {
  classifyReasons[filename] = reason
  message.success(`已完成「${filename}」初步解析，识别为${label}`)
}

function dropPending(card: PendingClassifyCard) {
  pendingClassify.value = pendingClassify.value.filter((c) => c.id !== card.id)
}

// 真正把文件送进对应上传管线——招标走 IntakeUploader.handleFile（唯一持有
// 上传+轮询+失败重试逻辑的地方，不重写），投标/清单走已有的
// onDropBidFiles/uploadExcel（同样不重写）。
//
// 每个 route* 都在**继任卡片已经存在之后**才 dropPending：先 drop 再 await
// 会留出一段"分析中卡片没了、投标卡片还没建"的空窗，界面上就是卡片闪一下
// 消失又出现，正是 req1 要消灭的那种看不出发生了什么的状态。
async function routeToTender(file: File, card: PendingClassifyCard) {
  if (!tenderUploaderRef.value) {
    // IntakeUploader 现在无条件挂载（模板里 display:none 的那个容器），走到
    // 这里说明组件树出了别的问题；保留防御分支，不该在真实交互中触发。
    card.error = '招标上传组件未就绪，请刷新页面重试'
    message.error(`「${file.name}」招标上传组件未就绪`)
    return
  }
  // 懒建项目之后这一句是必须的：以前靠 onMounted 预建的项目白蹭，
  // 现在没有了，招标上传自己要先把项目建出来（IntakeUploader 的 context
  // 需要 project_id）。
  await ensureProject()
  tenderFilename.value = file.name
  tenderError.value = ''
  tenderProgressPct.value = 1
  tenderStage.value = '上传中'
  tenderAutoRouting.value = true
  dropPending(card)   // tenderAutoRouting 已为真 → 招标卡片同一帧接上
  await tenderUploaderRef.value.handleFile(file)
}

async function routeToBid(file: File, card: PendingClassifyCard, kind: BidDocKind) {
  await ensureProject()
  handleBatchFile(file, kind)   // 同步 push 进 batchFiles
  dropPending(card)
}

async function routeToTenderList(file: File, card: PendingClassifyCard) {
  const p = uploadExcel(file)   // 同步设置 excelFilename/excelPreviewing
  dropPending(card)
  await p
}

// design/29 附加要求（2026-08-20）：PDF 判不出招标/投标时弹窗二选一，不再
// 是"提示一下、让用户自己去对应卡片重新拖"——那个台阶已经去掉了。
// 弹窗期间「分析中」卡片保留（换一行文案说明在等人确认），不是消失——文件
// 确实还在处理中，卡片消失等于告诉用户"这份没了"。
function askTenderOrBid(file: File, card: PendingClassifyCard, reasonHint: string) {
  card.note = '等待确认文件类型'
  Modal.confirm({
    title: `「${file.name}」看不出是招标文件还是投标文件`,
    content: reasonHint,
    okText: '招标文件',
    cancelText: '投标文件',
    onOk: () => routeToTender(file, card),
    onCancel: () => routeToBid(file, card, inferBidDocKind(file.name)),
  })
}

async function classifyAndRouteFile(file: File, card: PendingClassifyCard) {
  card.note = '判定文件类型…'
  const form = new FormData()
  form.append('file', file)
  let result
  try {
    const { data } = await intakeApi.classifyTier0(form)
    result = data
  } catch (e: unknown) {
    // design/29 §12：手动上传区域已撤掉，分类失败不能再把用户支到一个不存在
    // 的地方。落到跟"判不出来"同一条兜底路径——弹二选一，文件继续往下走，
    // 而不是停在一张死卡片上。超时是这里最常见的失败（见 §12.1），文案要说
    // 清楚是"没能在限时内判完"，不是"这文件有问题"。
    const timedOut = (e as { code?: string })?.code === 'ECONNABORTED'
    askTenderOrBid(file, card, timedOut
      ? '自动判定超时（后端可能仍在处理其它文件）。这份文件是招标方还是投标方提供的？'
      : '自动判定没能完成（接口异常）。这份文件是招标方还是投标方提供的？')
    return
  }

  if (result.kind === 'excel') {
    if (result.verdict === 'tender_list') {
      noteClassified(file.name, '采购清单', result.reason)
      await routeToTenderList(file, card)
    } else if (result.verdict === 'bid_list') {
      noteClassified(file.name, '报价清单',
        `价格列填充率 ${((result.fill_rate ?? 0) * 100).toFixed(0)}%。${result.reason}`)
      await routeToBid(file, card, 'bid_list')
    } else {
      askTenderOrBid(file, card, `Excel 是清单还是报价单不确定（${result.reason}），这份文件本身是招标方还是投标方提供的？`)
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
      noteClassified(file.name, '招标文件', result.reason)
      await routeToTender(file, card)
    } else if (result.verdict === 'bid') {
      noteClassified(file.name, '投标文件', result.reason)
      await routeToBid(file, card, 'bid')
    } else {
      askTenderOrBid(file, card, result.text_layer === 'scanned'
        ? `视觉判定信息不足以确定招投标类型（${result.reason || '未配置视觉客户端或识别信息不够'}），请人工确认。`
        : `文字层判据不够明确（${result.reason || '招投标关键词均未命中，或两侧都命中'}），请人工确认。`)
    }
    return
  }

  card.error = result.reason || '不支持的文件类型'
  message.warning(`「${file.name}」不支持的文件类型`)
}

// design/29 §14：分类请求的并发上限。**不是无上限、也不是串行**。
// 一次分类里 pdfium 渲染前几页占 1.2-1.6s（这段必须串行——所有渲染入口按
// `.claude/rules/recognition.md` 走同一把 `_PDF_LOCK`），视觉调用占 5-7s
// （这段可以并行）。所以并发 N 的尾部耗时 ≈ N×1.5s + 一次视觉调用。
//
// 取 8 = 后端识别线程池 `EXTRACTION_THREAD_POOL_SIZE` 的默认值：分类之后
// 紧接着就是识别，识别端只有 8 条线程，分类再快也会在那里排队，超过 8 的
// 并发买不到任何东西。日常一次拖 4-8 份，等于"拖几个并行几个"。
// 上限不能去掉：见 §14 记的那条——分类路由每个请求新建一个
// DashScopeOCRProvider，per-key 并发信号量因此是每实例一份，对这条路径
// 形同虚设，客户端这道闸是目前唯一挡在视觉 API 限流前面的东西。
const CLASSIFY_CONCURRENCY = 8
const classifyQueue: Array<() => Promise<void>> = []
let classifyActive = 0

function pumpClassifyQueue() {
  while (classifyActive < CLASSIFY_CONCURRENCY && classifyQueue.length) {
    const job = classifyQueue.shift()!
    classifyActive++
    job().finally(() => {
      classifyActive--
      pumpClassifyQueue()
    })
  }
}

async function onDropAnyFiles(file: File) {
  const card = addPending(file)
  // 超出并发上限的文件确实在排队，卡片如实说，不假装在"判定中"。
  if (classifyActive >= CLASSIFY_CONCURRENCY) card.note = '排队中，等待前面的文件判完'
  classifyQueue.push(() => classifyAndRouteFile(file, card))
  pumpClassifyQueue()
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
      // design/29 §10 req4/req5：招标单位既单独显示在徽标后，也进概述事实集。
      tenderer: result.tenderer,
      project_code: result.project_code || projectCode.value,
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
      // design/29 §10 req5"如果有报价合计最好，以便和下方解析人工核对"：
      // 明细逐行相加 与 文件自己声明的总价分别送，后端也分别陈述——两者
      // 不一致正是要人工核对的信号，合并成一个数就把信号抹掉了。
      quote_total: bidStatsFor(f).total,
      declared_total: f.declaredTotal ?? undefined,
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
    const it = item as unknown as Record<string, unknown>
    // design/29 §11.1：表头区分含税/不含税的报价（凯硕、泰科龙）里，通用
    // total_price 槽位**本来就该是空的**——值落在税基槽位。只读 total_price
    // 会把这类文件的合计算成 ¥0，卡片上正是这么显示的。改读口径已判定的有效
    // 合价（pipeline 的 derive_price_basis 产物），拿不到才退回原槽位。
    // 这只改显示读哪个键，不改任何原值，也不做跨口径换算。
    const v = Number(it.effective_total_price ?? it.total_price)
    return sum + (Number.isFinite(v) ? v : 0)
  }, 0)
  const pendingRows = new Set(doubtMarksFor(f.id).map((m) => m.row))
  return { count: f.items.length, total, pendingCount: pendingRows.size }
}

// ─── design/29 §10 req1-req6：统一卡片模型 ──────────────────────────────────
//
// 一份拖进来的文件 = 一张卡片，从落地到识别完成始终存在，只是徽标随判定结果
// 变化：分析中 → 招标文件 / 采购清单 / 投标文件 / 报价清单。
//
// 卡片形状与文案的判定逻辑在 utils/docCards.ts（纯函数、有单测）；这里只做
// 一件事：把四个状态源按顺序拼起来，再按 CARD_ORDER 排。
const docCards = computed<DocCard[]>(() => {
  const cards: DocCard[] = buildPendingCards(pendingClassify.value)

  if (tenderResult.value || tenderAutoRouting.value || tenderJob.value) {
    cards.push(buildTenderCard({
      filename: tenderFilename.value,
      classifyReason: classifyReasons[tenderFilename.value] || '',
      result: tenderResult.value,
      summary: tenderSummary.value,
      summaryLoading: tenderSummaryLoading.value,
      progressPct: tenderProgressPct.value,
      stage: tenderStage.value,
      error: tenderError.value,
    }))
  }

  if (excelFile.value || excelPreviewing.value) {
    cards.push(buildTenderListCard({
      filename: excelFilename.value,
      classifyReason: classifyReasons[excelFilename.value] || '',
      rowCount: excelRowCount.value,
      previewing: excelPreviewing.value,
      error: excelError.value,
    }))
  }

  for (const f of batchFiles.value) {
    cards.push(buildBidCard({
      id: f.id, filename: f.filename, kind: f.docKind, status: f.status,
      supplierName: f.finalSupplierName || f.detectedSupplierName,
      stage: f.stage, stageDetail: f.stageDetail, progressPct: f.progressPct,
      error: f.error,
      summary: supplierSummaries[f.id] || '',
      summaryLoading: !!supplierSummaryLoading[f.id],
      stats: f.status === 'done' ? bidStatsFor(f) : null,
      declaredTotal: f.declaredTotal,
      classifyReason: classifyReasons[f.filename] || '',
    }))
  }

  // 招标侧排最前：它是这一屏的基准——采购清单决定了矩阵有哪些行，投标卡片
  // 是拿来跟它比的。还在判类型的垫底，因为它们还不知道自己是什么。
  // sort 稳定（ES2019 起有保证），同档内保持上面的插入顺序。
  const rank: Record<DocCard['kind'], number> = {
    tender: 0, tender_list: 1, bid: 2, bid_list: 2, analyzing: 3,
  }
  return [...cards].sort((a, b) => rank[a.kind] - rank[b.kind])
})

// ─── Step 4「结果」：复用 BidMatrix.vue ────────────────────────────────────
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)
const confirmedSubmissionIds = computed(() =>
  batchFiles.value.filter((f) => f.confirmed && f.confirmedSubmissionId != null).map((f) => f.confirmedSubmissionId!))

// design/31：还没逐行确认时点"开始比价分析"，走预览口径——先看个大概，
// 再按"确认哪一行最能改变结论"去确认，而不是逼人先把 89 行看完。
const previewResult = ref<BidMatrixPreviewResult | null>(null)
/** 展开原文依据的队列项下标；null = 全部收起。一次只展开一条，
 *  这几条本来就是要逐条看的，全展开会把队列淹掉。 */
const expandedEvidence = ref<number | null>(null)

/** 已识别完成、还没入库的文件——预览要吃的就是这批。 */
const previewableFiles = computed(() =>
  batchFiles.value.filter(f => f.status === 'done' && !f.confirmed
                               && !!f.jobId && !!f.finalSupplierName.trim()))

function onRetryCard(fileId: string) {
  const f = batchFiles.value.find(x => x.id === fileId)
  if (f) retryBatchFile(f)
}

// design/32 §10：招标清单 → 比价基准（TenderListSession）。
const baselineConfirmed = ref(false)
const confirmingBaseline = ref(false)

/** 打开已有项目时，先问后端这个项目有没有已确认基准，别让按钮误显示。 */
async function refreshBaselineState() {
  if (!projectId.value) { baselineConfirmed.value = false; return }
  try {
    const { data } = await analysisApi.tenderListCurrentSessions({ project_id: projectId.value })
    baselineConfirmed.value = (data.sessions || []).length > 0
  } catch {
    // 查不到就按"未确认"显示。宁可多显示一个按钮（点了是幂等的），
    // 也不要因为一次查询失败让用户以为已经确认过。
    baselineConfirmed.value = false
  }
}

async function confirmBaseline() {
  if (!projectId.value || !tenderResult.value) return
  const r = tenderResult.value
  confirmingBaseline.value = true
  try {
    await analysisApi.tenderListConfirm({
      project_id: projectId.value,
      category: category.value || r.detected_category || r.material_class,
      file_name: tenderFilename.value,
      anchors_json: r.items,
      anchors_total: r.row_count,
      source_type: r.source_type,
      brand_requirement: r.brand_requirement,
      supplier_brands: r.supplier_brands,
    })
    baselineConfirmed.value = true
    message.success(`已确认 ${r.row_count} 项为比价基准`)
    // 基准变了，之前那份按报价派生轴算的预览就过期了——留着会让人以为
    // 它还代表当前口径。清掉，让用户重新点一次。
    previewResult.value = null
  } catch (e: unknown) {
    message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      || '确认比价基准失败')
  } finally {
    confirmingBaseline.value = false
  }
}

async function runPreview() {
  if (!projectId.value) return
  analyzing.value = true
  previewResult.value = null
  try {
    const { data } = await analysisApi.bidMatrixPreview({
      project_id: projectId.value,
      category: category.value,
      confirmations: previewableFiles.value.map(f => ({
        job_id: f.jobId as string,
        supplier_id: f.matchedSupplierId ?? undefined,
        supplier_name: f.finalSupplierName.trim(),
        project_id: projectId.value,
        category: category.value,
        overrides: f.items as unknown as Array<Record<string, unknown>>,
        bid_status: taskConfig.bidStatus,
      })),
    })
    previewResult.value = data
    // 预览矩阵也塞进 matrixResult 让 BidMatrix 组件渲染——同一个组件、同一份
    // 数据形状，上方横幅负责说清这是预览口径。
    matrixResult.value = data.matrix
  } catch (e: unknown) {
    message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '预览比价失败')
  } finally {
    analyzing.value = false
  }
}

async function runAnalysis() {
  if (!projectId.value) return
  if (confirmedSubmissionIds.value.length === 0) {
    if (previewableFiles.value.length > 0) return runPreview()
    message.warning('还没有可比价的报价——请先拖入投标文件或报价清单')
    return
  }
  previewResult.value = null
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
        <div v-if="metaSaveError" class="workspace-header__meta-error">
          {{ metaSaveError }}
        </div>
      </div>
      <div class="workspace-header__actions">
        <a-button @click="goToAlignment">
          <SolutionOutlined />对齐核查
        </a-button>
        <a-button><HistoryOutlined />历史</a-button>
        <!-- design/31 cut 4：预览态禁用导出。**不是因为预览数据会被导出**
             ——导出是从库里按已确认报价重算的，预览一个字节都没落库，泄漏
             不可能发生（apps/api/tests/test_export_excludes_preview.py）。
             要挡的是反过来那件事：屏幕上摆着一份含 N 家的预览矩阵，点导出
             拿到的却是一份按已确认口径重算的、数字完全不同的表（实测：一家
             都没确认时是一张空表），中间没有任何提示。宁可点不动，也不给一
             份跟屏幕对不上的文件。 -->
        <a-tooltip v-if="previewResult"
                   title="当前是预览口径（含未确认报价）。导出只输出已确认的报价，会与屏幕上的结果不一致——请先完成校对入库再导出。">
          <a-button type="primary" disabled><DownloadOutlined />导出</a-button>
        </a-tooltip>
        <a-button v-else type="primary"><DownloadOutlined />导出</a-button>
      </div>
    </div>

    <!-- design/28 cut 5 + design/29 §1/§3：拖一堆文件进来自动分类，是唯一
         上传入口——Excel 是确定性判据（无价格列→采购清单，填满→报价清单），
         PDF 原生文字层有真实判据可直接路由，扫描件/判不出来的一律弹窗二选一
         （不再是"提示一下让你自己去对应卡片重新拖"）。 -->
    <!-- 外面这层 div 是必需的：margin 直接写在 .auto-classify-dragger 上不生效
         —— AntD v4 的 a-upload-dragger 外面还包了一个 .ant-upload-wrapper，
         我们的 class 落在内层元素上，它的 margin 撑不开外层。用一个自己的
         容器来管间距，不依赖组件库的内部结构。 -->
    <div class="upload-zone">
    <a-upload-dragger
      :show-upload-list="false" :multiple="true"
      accept=".pdf,.xlsx,.xls,.png,.jpg,.jpeg"
      :before-upload="onDropAnyFiles"
      class="auto-classify-dragger"
    >
      <p class="ant-upload-drag-icon"><InboxOutlined style="font-size:32px;color:#1677ff" /></p>
      <p class="ant-upload-text" style="font-size:14px">拖入招标文件（PDF）、投标文件（PDF）或采购清单（Excel）、报价清单（Excel）</p>
      <p class="ant-upload-hint" style="font-size:12px">
        <LoadingOutlined v-if="pendingClassify.length > 0" spin />
        {{ pendingClassify.length > 0 ? `正在判定 ${pendingClassify.length} 个文件的类型…` : '自动识别归类；判不出来时会弹窗让你确认一下' }}
      </p>
      <!-- design/29 §10 req1：这个数字必须跟下方卡片数一一对应，用户才能
           拿它核对"我拖了 5 个，是不是都在处理"。 -->
      <p v-if="uploadedFileCount > 0" class="ant-upload-hint" style="font-size:12px;margin-top:4px">
        共 {{ uploadedFileCount }} 个文件，下方 {{ uploadedFileCount }} 张卡片
      </p>
    </a-upload-dragger>
    </div>

    <!-- design/29 §12（2026-08-21 手测反馈）：三张手动上传卡片全部撤掉。
         统一拖拽区加上"一份文件一张卡片"之后，它们是同一件事的第二个入口
         ——用户实测反馈"没有必要，容易造成困惑"。IntakeUploader **仍然挂载**
         （招标文件的上传/轮询/失败重试逻辑只有它有，统一拖拽区通过 ref 程序
         化调用它），只是不再画自己那套 dragger + 进度块：进度已经由招标卡片
         显示，两处画同一个进度正是困惑的来源。 -->
    <div style="display:none">
      <IntakeUploader
        ref="tenderUploaderRef"
        type="tender_bidlist"
        :context="uploaderContext"
        @extracted="onTenderExtracted"
        @failed="onTenderFailed"
        @progress="onTenderProgress"
      />
    </div>

    <!-- design/29 §2/§4/§5 + §10 req1-req6：卡片区是默认唯一视图。
         一份文件一张卡片（含还在判定类型的），徽标 = 四类之一或「分析中」，
         徽标后是单位名称（大字，只有名称），下面是 LLM 概述与"多少项"。
         明细表格（含"确认入库"）是点卡片之后的下一步，不在这一屏。 -->
    <!-- TransitionGroup 而不是 v-for + div：招标文件判定完成后会从"分析中"
         那一档跳到第一位，位置变化用 FLIP 动画交代清楚（card-move），否则
         卡片凭空跳一下，看的人不知道刚才发生了什么。 -->
    <TransitionGroup v-if="docCards.length > 0" name="card" tag="div" class="summary-cards">
      <div
        v-for="c in docCards" :key="c.id"
        class="summary-card" :class="[`summary-card--${c.kind}`, { 'summary-card--static': !c.detailKey }]"
        @click="c.detailKey && openDetail(c.detailKey)"
      >
        <div class="summary-card__badge" :class="`summary-card__badge--${c.kind}`"
             :title="c.badgeTooltip || undefined">
          <LoadingOutlined v-if="c.kind === 'analyzing' && !c.errorText" spin />
          {{ CARD_KIND_LABEL[c.kind] }}
        </div>
        <div class="summary-card__body">
          <!-- req4：单位名称独占一行、字体更大，只放名称本身。 -->
          <div v-if="c.unitName" class="summary-card__unit">{{ c.unitName }}</div>
          <div v-else-if="c.unitMissingNote" class="summary-card__unit summary-card__unit--missing">
            {{ c.unitMissingNote }}
          </div>
          <div class="summary-card__filename">{{ c.filename }}</div>

          <a-spin v-if="c.summaryLoading" size="small" style="margin-top:6px" />
          <div v-else-if="c.summary" class="summary-card__text">{{ c.summary }}</div>

          <!-- design/32 §10：招标清单识别出来 ≠ 已成为比价基准。
               旧 5 步向导（design/27 步骤5 退役）是 tenderListConfirm 唯一的
               调用者，删掉之后这一步在界面上**没有任何入口**——招标清单永远
               变不成 TenderListSession，官方矩阵/导出/预览全都以为"没有采购
               清单"。用户连撞两次才发现。这个按钮就是把那一步补回来。 -->
          <div v-if="c.kind === 'tender' && tenderResult && (tenderResult.row_count || 0) > 0"
               class="summary-card__baseline">
            <template v-if="baselineConfirmed">
              <span class="summary-card__baseline-ok">✓ 已作为比价基准（{{ tenderResult.row_count }} 项）</span>
            </template>
            <template v-else>
              <a-button type="primary" size="small" :loading="confirmingBaseline"
                        @click.stop="confirmBaseline">确认为比价基准</a-button>
              <span class="summary-card__baseline-hint">
                确认后这 {{ tenderResult.row_count }} 项成为比价的行轴；在此之前比价只能按报价互相对齐
              </span>
            </template>
          </div>
          <div v-if="c.statsText" class="summary-card__stats">
            {{ c.statsText }}
            <span v-if="c.pendingText" class="summary-card__stats-pending">{{ c.pendingText }}</span>
          </div>

          <div v-if="c.errorText" class="summary-card__stats summary-card__stats--error">
            {{ c.errorText }}
            <a-button v-if="c.retryKey" size="small" type="link"
                      @click.stop="onRetryCard(c.retryKey)">重试</a-button>
          </div>
          <template v-else-if="c.progressPct !== null">
            <a-progress :percent="c.progressPct" size="small" status="active" />
            <div class="summary-card__stats">{{ c.stageText }}</div>
          </template>
        </div>
      </div>
    </TransitionGroup>

    <!-- 明细 + 确认入库——下一步，默认不显示（点卡片才进来，见 openDetail）。 -->
    <template v-if="viewMode === 'detail'">
      <a-button size="small" class="detail-back" @click="viewMode = 'overview'">← 返回概述</a-button>
      <a-tabs v-model:active-key="activeTab" class="workspace-tabs">
        <a-tab-pane key="list">
          <template #tab>清单{{ tenderResult ? ` · ${tenderResult.row_count} 项` : '' }}</template>
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
        {{ matrixResult ? '重新分析' : (confirmedSubmissionIds.length ? '开始比价分析' : '先比价看看（预览）') }}
      </a-button>

      <!-- design/31 §5：预览口径必须自己说清楚是预览。横幅不是装饰——
           这份矩阵长得跟正式结果一模一样，不写就没有任何东西能区分它们。 -->
      <template v-if="previewResult">
        <a-alert type="warning" show-icon banner class="preview-banner">
          <template #message>
            预览口径 · 含 {{ previewResult.matrix.preview_unconfirmed_rows ?? 0 }} 行未确认报价，不作为定标依据
          </template>
          <template #description>{{ previewResult.summary }}</template>
        </a-alert>

        <!-- design/32 §5：行轴来自报价自己（没有采购清单）时，这份结果的
             证据强度跟"有采购清单"完全不是一回事——只是黄色横幅还说明不了
             这个区别（那条讲的是"数据未确认"，这条讲的是"没有招标依据可
             对照"），必须单独说清楚，不能只塞进 notes 里的一行小字。 -->
        <a-alert v-if="previewResult.matrix.axis_kind === 'quote_derived'"
                 type="info" show-icon banner class="preview-banner">
          <template #message>未提供采购清单，比价基准取自本轮报价本身</template>
          <template #description>
            只能看出各家同一行报价是否不同；无法判断是否有招标要求的项目被漏报——
            没有招标清单，就没有「应该有什么」的依据。
          </template>
        </a-alert>

        <div v-if="previewResult.notes.length" class="preview-notes">
          <div v-for="(n, i) in previewResult.notes" :key="i">· {{ n }}</div>
        </div>

        <div v-if="previewResult.queue.length" class="preview-queue">
          <div class="preview-queue__title">
            建议按这个顺序确认（{{ previewResult.queue.length }} 处）
            <span v-if="previewResult.unbounded_count" class="preview-queue__warn">
              其中 {{ previewResult.unbounded_count }} 处无法估算影响，排在最前
            </span>
          </div>
          <div v-for="(q, i) in previewResult.queue.slice(0, 20)" :key="i" class="preview-queue__item">
            <div class="preview-queue__row">
              <span class="preview-queue__anchor">#{{ q.anchor_key }}</span>
              <span class="preview-queue__supplier">{{ q.supplier_key }}</span>
              <!-- unbounded 的 swing 是 null，绝不显示成 ¥0：那读起来是"影响很小"，
                   而实际含义是"无从判断"，方向正好相反。 -->
              <span v-if="q.kind === 'unbounded'" class="preview-queue__warn">影响无从估算（同行报价不足）</span>
              <span v-else class="preview-queue__swing">可能影响 ¥{{ formatMoney(q.swing || 0) }}</span>
              <a-button v-if="q.evidence" size="small" type="link"
                        @click="expandedEvidence = expandedEvidence === i ? null : i">
                {{ expandedEvidence === i ? '收起原文依据' : '看原文依据' }}
              </a-button>
            </div>
            <!-- design/32 §11：不让用户去翻纸质件。系统手里有 source_ref，
                 把「原文第几页第几行」和「那一行我们识别到了什么」摆出来，
                 哪个字段是空的一眼就看见。 -->
            <div v-if="q.evidence && expandedEvidence === i" class="preview-evidence">
              <div class="preview-evidence__loc">
                原文位置：第 {{ q.evidence.page ?? '?' }} 页 · 第 {{ q.evidence.row ?? '?' }} 行
                <span class="preview-evidence__caveat">
                  （以下是系统识别到的内容，不是原文影像——够判断哪个字段没读到，
                  不足以证明原文就是这个值）
                </span>
              </div>
              <div class="preview-evidence__grid">
                <span>名称</span><b>{{ q.evidence.raw_name || '—' }}</b>
                <span>规格</span><b>{{ q.evidence.spec || '—' }}</b>
                <span>单位</span><b>{{ q.evidence.unit || '—' }}</b>
                <span>数量</span><b :class="{ 'preview-evidence__missing': q.evidence.qty == null }">
                  {{ q.evidence.qty ?? '未读到' }}</b>
                <span>单价</span><b :class="{ 'preview-evidence__missing': q.evidence.unit_price == null }">
                  {{ q.evidence.unit_price ?? '未读到' }}</b>
                <span>单价(不含税)</span><b :class="{ 'preview-evidence__missing': q.evidence.unit_price_excl_tax == null }">
                  {{ q.evidence.unit_price_excl_tax ?? '未读到' }}</b>
                <span>合价</span><b :class="{ 'preview-evidence__missing': q.evidence.total_price == null }">
                  {{ q.evidence.total_price ?? '未读到' }}</b>
                <span>税率</span><b>{{ q.evidence.tax_rate ?? '—' }}</b>
              </div>
              <div v-if="q.evidence.pending_note" class="preview-evidence__note">
                系统标记的疑点：{{ q.evidence.pending_note }}
              </div>
            </div>
          </div>
          <div v-if="previewResult.queue.length > 20" class="preview-queue__more">
            仅列出影响最大的 20 处，共 {{ previewResult.queue.length }} 处
          </div>
        </div>
      </template>
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
.workspace-header__meta-error { font-size: 12px; color: #cf1322; margin-top: 4px; }
.workspace-header__actions { display: flex; gap: 8px; flex-shrink: 0; }

/* design/28 cut 5 自动分类拖拽区——视觉上比下方三张精确卡片更突出（更高、
   更亮的强调色），暗示"这是首选入口，下面三张是需要手动指定类型时的备选"。 */
/* 24px 而不是跟卡片之间一样的 16px：这里分隔的是「上传区」和「结果区」
   两个功能块，比同类卡片之间的间距大一档，层次才读得出来。间距挂在自己的
   容器上，不挂在 dragger 上（见模板里的注释）。 */
.upload-zone { margin-bottom: 24px; }
.auto-classify-dragger { margin-bottom: 0; border-color: #91caff; background: #f0f7ff; }
.auto-classify-dragger :deep(.ant-upload-drag-icon) { margin-bottom: 6px; }

/* Materials strip：三张等宽卡片，不是三个内联按钮——每张卡片自己决定内容
   （上传态用 dragger，完成态用摘要），卡片本身的边框/圆角/内边距统一。 */

/* 2026-08-21 手测反馈：卡片改纵向、100% 宽，不再横排（原来 flex-wrap 横排
   在窄屏/多供应商时挤成一团，也不是"先看概述"该有的阅读顺序）。
   间距统一走 8px 栅格（16/24），卡片之间不再是 12px 这种半档值。 */
.summary-card__baseline { display: flex; align-items: center; gap: 12px; margin-top: 8px; flex-wrap: wrap; }
.summary-card__baseline-hint { font-size: 12px; color: rgba(0,0,0,0.45); }
.summary-card__baseline-ok { font-size: 13px; color: #389e0d; font-weight: 500; }
.preview-banner { margin-top: 16px; }
.preview-notes { margin-top: 8px; font-size: 12px; color: rgba(0,0,0,0.45); line-height: 1.8; }
.preview-queue { margin-top: 16px; border: 1px solid #ffe58f; border-radius: 8px; padding: 16px; background: #fffbe6; }
.preview-queue__title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.preview-queue__warn { color: #d46b08; font-weight: 500; }
.preview-queue__row { display: flex; gap: 16px; align-items: baseline; font-size: 13px; }
.preview-queue__anchor { flex-shrink: 0; min-width: 48px; color: rgba(0,0,0,0.45); font-variant-numeric: tabular-nums; }
.preview-queue__supplier { flex-shrink: 0; min-width: 96px; font-weight: 500; }
.preview-queue__swing { color: rgba(0,0,0,0.65); font-variant-numeric: tabular-nums; }
.preview-queue__item { padding: 4px 0; }
.preview-evidence { margin: 4px 0 8px 64px; padding: 12px; background: #fff; border: 1px solid #ffe58f; border-radius: 6px; }
.preview-evidence__loc { font-size: 12px; color: rgba(0,0,0,0.65); margin-bottom: 8px; }
.preview-evidence__caveat { color: rgba(0,0,0,0.45); }
.preview-evidence__grid { display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 4px 12px; font-size: 13px; align-items: baseline; }
.preview-evidence__grid span { color: rgba(0,0,0,0.45); }
.preview-evidence__missing { color: #cf1322; font-weight: 600; }
.preview-evidence__note { margin-top: 8px; font-size: 12px; color: #d46b08; }
.preview-queue__more { margin-top: 8px; font-size: 12px; color: rgba(0,0,0,0.45); }

.summary-cards { display: flex; flex-direction: column; gap: 16px; margin-bottom: 24px; }
.summary-card {
  width: 100%;
  border: 1px solid #e8e8e8; border-radius: 8px; padding: 16px;
  background: #fff; cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s, background 0.3s;
  display: flex; gap: 16px;
}
.summary-card:hover { border-color: #1677ff; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
/* 招标侧给底色，跟投标卡片分开：这一屏里招标文件是"基准"，投标是"待比"，
   两者不是同一类东西。色相跟徽标同源（暖红/暖橙），不另起一套配色。 */
.summary-card--tender { background: #fffbfa; border-color: #ffd8d3; }
.summary-card--tender:hover { border-color: #ff7875; }
.summary-card--tender_list { background: #fffdf7; border-color: #ffe1ab; }
.summary-card--tender_list:hover { border-color: #ffa940; }

/* 招标文件判定完后从"分析中"档跳到第一位——位移用 FLIP 动画交代，
   并给它一下高亮，让"这张卡片刚被认出来并提到了最前面"是看得见的。 */
.card-move { transition: transform 0.45s cubic-bezier(0.4, 0, 0.2, 1); }
.card-enter-active { transition: opacity 0.3s ease, transform 0.3s ease; }
.card-leave-active { transition: opacity 0.2s ease; position: absolute; }
.card-enter-from { opacity: 0; transform: translateY(-8px); }
.card-leave-to { opacity: 0; }
@media (prefers-reduced-motion: reduce) {
  .card-move, .card-enter-active, .card-leave-active { transition: none; }
}
.summary-card__badge {
  flex-shrink: 0; align-self: flex-start;
  font-size: 12px; font-weight: 500; padding: 2px 8px; border-radius: 4px;
}
/* 四类徽标各一色 + 分析中一色（design/29 §10 req2/req3）。招标侧暖色、
   投标侧冷色，清单是各自的浅色版——同一方的两种文件一眼能归到一起。 */
.summary-card__badge--tender { background: #fff1f0; color: #cf1322; }
.summary-card__badge--tender_list { background: #fff7e6; color: #d46b08; }
.summary-card__badge--bid { background: #e6f4ff; color: #1677ff; }
.summary-card__badge--bid_list { background: #f0f5ff; color: #2f54eb; }
.summary-card__badge--analyzing { background: #f5f5f5; color: rgba(0,0,0,0.45); }
/* 还判不出类型/已入库的卡片点开没有东西可看，不给点击手势——鼠标变手型
   却点不动比不变手型更让人困惑。 */
.summary-card--static { cursor: default; }
.summary-card--static:hover { border-color: #e8e8e8; box-shadow: none; }
.summary-card__body { flex: 1; min-width: 0; }
/* req4：单位名称"字体大一些"，且只显示单位名称。 */
.summary-card__unit { font-size: 16px; font-weight: 600; color: rgba(0,0,0,0.88); line-height: 1.4; }
.summary-card__unit--missing { font-size: 13px; font-weight: 400; color: rgba(0,0,0,0.35); font-style: italic; }
.summary-card__filename { font-size: 12px; color: rgba(0,0,0,0.45); margin-top: 2px; word-break: break-all; }
.summary-card__text {
  font-size: 13px; color: rgba(0,0,0,0.85); line-height: 1.5; margin-top: 6px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.summary-card__stats { font-size: 12px; color: rgba(0,0,0,0.45); margin-top: 6px; }
.summary-card__stats-pending { color: #d46b08; }
.summary-card__stats--error { color: #ff4d4f; }

.detail-back { margin-bottom: 12px; }
.workspace-tabs { background: #fff; }
.supplier-tab-content { padding: 8px 0; }
.supplier-tab-content__progress { padding: 24px; text-align: center; color: rgba(0,0,0,0.55); }
.supplier-tab-content__meta { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.result-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid #f0f0f0; }
</style>
