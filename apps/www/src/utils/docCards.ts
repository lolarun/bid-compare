/**
 * docCards.ts — design/29 §10 req1-req6：工作台"一份文件一张卡片"的投影。
 *
 * 卡片**不是**新的状态机，是四个已有状态源（待分类队列 / 招标 IntakeUploader /
 * 采购清单 Excel 预览 / batchFiles）的只读投影。不另存一份卡片状态是有意的：
 * 两份状态必然漂移，界面就会跟真实进度对不上，那正是这轮要修的问题本身。
 *
 * 逻辑放在这里而不是 WorkspaceView.vue 的 computed 里，是为了能直接对
 * "徽标是什么 / 单位名称显示什么 / 计量词是不是「项」"写断言——这几条正是
 * 用户逐条提出的验收点，埋在 SFC 里就只能靠手测复述。
 */

export type CardKind = 'analyzing' | 'tender' | 'tender_list' | 'bid' | 'bid_list'

export const CARD_KIND_LABEL: Record<CardKind, string> = {
  analyzing: '分析中',
  tender: '招标文件',
  tender_list: '采购清单',
  bid: '投标文件',
  bid_list: '报价清单',
}

export interface DocCard {
  id: string
  kind: CardKind
  filename: string
  /**
   * req4：徽标后面的单位名称——招标侧是招标单位，投标侧是投标单位，**只放
   * 单位名称**，别的信息一律不塞进这一行。抽不到时留空并由
   * `unitMissingNote` 说明为什么缺，不拿项目名/文件名冒充（design/27 红线1：
   * 只陈述已知事实）。
   */
  unitName: string
  unitMissingNote: string
  /**
   * 分类判据原文（`classify-tier0` 的 reason）。design/29 §13：这段证据
   * 曾经整段塞进 toast，实测是一屏文字、没人读得完。改成挂在徽标上鼠标
   * 悬停可看——**不是删掉**：判据是"为什么系统这么判"的唯一说明，删了就
   * 只剩一个无从质疑的结论（design/27 红线1）。
   */
  badgeTooltip: string
  summary: string
  summaryLoading: boolean
  statsText: string
  pendingText: string
  progressPct: number | null    // null = 不显示进度条
  stageText: string
  errorText: string
  detailKey: string | null      // 非空 = 卡片可点开明细
  /**
   * 非空 = 这张卡片能重试（值是 batchFiles 的 id）。
   * 只在"读不到状态"这种失败上给——识别本身被服务端判失败时重试没有意义，
   * 那要重新上传，是另一件事。
   */
  retryKey: string | null
}

/** 待分类队列里的一份文件（WorkspaceView 持有，这里只读）。 */
export interface PendingClassifyCard {
  id: string
  filename: string
  note: string
  error: string
}

export function formatMoney(v: number): string {
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

/** req1/req2：还没判出类型的文件也要有卡片，徽标写「分析中」。 */
export function buildPendingCards(pending: readonly PendingClassifyCard[]): DocCard[] {
  return pending.map((p) => ({
    id: p.id,
    kind: 'analyzing' as const,
    filename: p.filename,
    unitName: '',
    unitMissingNote: '',
    badgeTooltip: '',
    summary: '',
    summaryLoading: false,
    statsText: '',
    pendingText: '',
    progressPct: p.error ? null : 0,
    stageText: p.error ? '' : p.note,
    errorText: p.error,
    detailKey: null,
    retryKey: null,
  }))
}

export interface TenderCardInput {
  filename: string
  classifyReason?: string
  /** 识别完成后的结果；null = 还在上传/识别中。 */
  result: { tenderer?: string; row_count: number } | null
  summary: string
  summaryLoading: boolean
  progressPct: number
  stage: string
  error: string
}

export function buildTenderCard(input: TenderCardInput): DocCard {
  const r = input.result
  return {
    id: 'card-tender',
    kind: 'tender',
    filename: input.filename || '招标文件',
    unitName: r?.tenderer || '',
    // "还没识别完"与"识别完了但原文没写"是两件事，只有后者才说"未识别到"。
    unitMissingNote: r && !r.tenderer ? '未识别到招标单位' : '',
    badgeTooltip: input.classifyReason || '',
    summary: input.summary,
    summaryLoading: input.summaryLoading,
    statsText: r ? `采购清单 ${r.row_count} 项` : '',
    pendingText: '',
    progressPct: r ? null : input.progressPct,
    stageText: r ? '' : (input.stage || '识别中'),
    errorText: input.error,
    detailKey: r ? 'list' : null,
    retryKey: null,
  }
}

export interface TenderListCardInput {
  filename: string
  classifyReason?: string
  rowCount: number | null
  previewing: boolean
  error: string
}

export function buildTenderListCard(input: TenderListCardInput): DocCard {
  return {
    id: 'card-tender-list',
    kind: 'tender_list',
    filename: input.filename || '采购清单',
    unitName: '',
    // Excel 采购清单里本来就没有单位字段——这不是"没识别出来"，是原文没有，
    // 两种情况的文案必须分得开（design/27 §3.1 feedback #2）。
    unitMissingNote: '清单文件不含单位信息',
    badgeTooltip: input.classifyReason || '',
    summary: '',
    summaryLoading: false,
    statsText: input.rowCount != null ? `采购清单 ${input.rowCount} 项` : '',
    pendingText: '',
    progressPct: input.previewing ? 50 : null,
    stageText: input.previewing ? '解析中' : '',
    errorText: input.error,
    detailKey: null,
    retryKey: null,
  }
}

export interface BidCardInput {
  id: string
  filename: string
  kind: 'bid' | 'bid_list'
  status: 'uploading' | 'processing' | 'done' | 'failed'
  supplierName: string
  stage: string
  stageDetail: string
  progressPct: number
  error: string
  summary: string
  summaryLoading: boolean
  /** 识别完成后的明细统计；status !== 'done' 时为 null。 */
  stats: { count: number; total: number; pendingCount: number } | null
  /** 文件自己声明的投标总价（`_doc_meta.bid_total`），拿不到时 null。 */
  declaredTotal: number | null
  /** 分类判据原文，挂在徽标上悬停可看。 */
  classifyReason?: string
}

export function buildBidCard(input: BidCardInput): DocCard {
  const done = input.status === 'done'
  const failed = input.status === 'failed'
  const stats = done ? input.stats : null
  // req5：声明总价与明细合计并列陈述，绝不合并成一个"总价"——两者不一致
  // 正是要人工核对的信号（checksum 门同一个道理），糊成一个数就把信号抹掉。
  // req6：计量词是「项」不是「行」。
  const statsText = stats
    ? `报价清单 ${stats.count} 项 · 明细合计 ¥${formatMoney(stats.total)}`
      + (input.declaredTotal != null ? ` · 文件声明总价 ¥${formatMoney(input.declaredTotal)}` : '')
    : ''
  return {
    id: input.id,
    kind: input.kind,
    filename: input.filename,
    unitName: input.supplierName,
    unitMissingNote: done && !input.supplierName ? '未识别到投标单位' : '',
    badgeTooltip: input.classifyReason || '',
    summary: input.summary,
    summaryLoading: input.summaryLoading,
    statsText,
    pendingText: stats && stats.pendingCount > 0
      ? `（含 ${stats.pendingCount} 项待确认，未计入官方评估）`
      : '',
    progressPct: done || failed ? null : input.progressPct,
    stageText: done || failed
      ? ''
      : `${input.stage || '分析中'}${input.stageDetail ? `（${input.stageDetail}）` : ''}`,
    errorText: failed ? (input.error || '识别失败') : '',
    detailKey: done ? input.id : null,
    // 只有"连接中断/读不到状态"能重试——服务端明确判定的识别失败重试也是
    // 同样的结果，给一个点了没用的按钮比不给更糟。
    retryKey: failed && input.error.includes('连接中断') ? input.id : null,
  }
}
