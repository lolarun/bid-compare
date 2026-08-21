/**
 * design/29 §10 req1-req6 的验收断言。这一批是用户逐条提出的界面要求，
 * 埋在 SFC 的 computed 里就只能靠手测复述——放在纯函数上直接断言。
 */
import { describe, it, expect } from 'vitest'
import {
  CARD_KIND_LABEL,
  buildPendingCards,
  buildTenderCard,
  buildTenderListCard,
  buildBidCard,
} from '../utils/docCards'

describe('req1/req2：每份文件一张卡片，判不出类型时徽标是「分析中」', () => {
  it('待分类的 N 份文件产出 N 张卡片，不是一个计数', () => {
    const cards = buildPendingCards([
      { id: 'p1', filename: 'a.pdf', note: '判定文件类型…', error: '' },
      { id: 'p2', filename: 'b.xlsx', note: '判定文件类型…', error: '' },
      { id: 'p3', filename: 'c.pdf', note: '判定文件类型…', error: '' },
    ])
    expect(cards).toHaveLength(3)
    expect(cards.map((c) => c.filename)).toEqual(['a.pdf', 'b.xlsx', 'c.pdf'])
  })

  it('类型未判出时徽标文案就是「分析中」', () => {
    const [card] = buildPendingCards([{ id: 'p1', filename: 'a.pdf', note: '判定文件类型…', error: '' }])
    expect(card.kind).toBe('analyzing')
    expect(CARD_KIND_LABEL[card.kind]).toBe('分析中')
    expect(card.stageText).toBe('判定文件类型…')
  })

  it('分类失败的卡片显示错误、不再显示进度条（否则像还在跑）', () => {
    const [card] = buildPendingCards([
      { id: 'p1', filename: 'a.pdf', note: '判定文件类型…', error: '分类接口异常' },
    ])
    expect(card.errorText).toBe('分类接口异常')
    expect(card.progressPct).toBeNull()
  })
})

describe('req3：徽标四类', () => {
  it('四个类别各有中文标签，跟用户给的词一致', () => {
    expect(CARD_KIND_LABEL.tender).toBe('招标文件')
    expect(CARD_KIND_LABEL.tender_list).toBe('采购清单')
    expect(CARD_KIND_LABEL.bid).toBe('投标文件')
    expect(CARD_KIND_LABEL.bid_list).toBe('报价清单')
  })

  it('Excel 报价清单与 PDF 投标文件是两个徽标，不混成一个', () => {
    const base = {
      id: 'f1', filename: 'x', status: 'done' as const, supplierName: '某供应商',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 3, total: 100, pendingCount: 0 }, declaredTotal: null,
    }
    expect(buildBidCard({ ...base, kind: 'bid' }).kind).toBe('bid')
    expect(buildBidCard({ ...base, kind: 'bid_list' }).kind).toBe('bid_list')
  })
})

describe('req4：徽标后显示单位名称，且只显示单位名称', () => {
  it('招标卡片用招标单位，不用项目名顶替', () => {
    const card = buildTenderCard({
      filename: '某项目-招标文件.pdf',
      result: { tenderer: '某某建设集团有限公司', row_count: 89 },
      summary: '', summaryLoading: false, progressPct: 100, stage: '', error: '',
    })
    expect(card.unitName).toBe('某某建设集团有限公司')
    expect(card.unitMissingNote).toBe('')
  })

  it('识别完成但原文没写招标单位 → 明说未识别到，不编一个名字', () => {
    const card = buildTenderCard({
      filename: 'a.pdf', result: { tenderer: '', row_count: 12 },
      summary: '', summaryLoading: false, progressPct: 100, stage: '', error: '',
    })
    expect(card.unitName).toBe('')
    expect(card.unitMissingNote).toBe('未识别到招标单位')
  })

  it('还在识别中时不说"未识别到"——那是两件事', () => {
    const card = buildTenderCard({
      filename: 'a.pdf', result: null,
      summary: '', summaryLoading: false, progressPct: 40, stage: '识别采购清单', error: '',
    })
    expect(card.unitMissingNote).toBe('')
    expect(card.progressPct).toBe(40)
    expect(card.stageText).toBe('识别采购清单')
  })

  it('投标卡片用投标单位名称', () => {
    const card = buildBidCard({
      id: 'f1', filename: 'x.pdf', kind: 'bid', status: 'done',
      supplierName: '某某机电设备有限公司',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 5, total: 10, pendingCount: 0 }, declaredTotal: null,
    })
    expect(card.unitName).toBe('某某机电设备有限公司')
  })

  it('采购清单 Excel 没有单位字段——说明原文没有，不说"未识别到"', () => {
    const card = buildTenderListCard({ filename: 'l.xlsx', rowCount: 89, previewing: false, error: '' })
    expect(card.unitName).toBe('')
    expect(card.unitMissingNote).toBe('清单文件不含单位信息')
  })
})

describe('req5：报价合计与文件声明总价分别陈述', () => {
  it('两个总价都有时并列显示，不合并成一个数', () => {
    const card = buildBidCard({
      id: 'f1', filename: 'x.pdf', kind: 'bid', status: 'done', supplierName: '某供应商',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 89, total: 1234567.89, pendingCount: 0 },
      declaredTotal: 1234000,
    })
    expect(card.statsText).toContain('明细合计')
    expect(card.statsText).toContain('文件声明总价')
  })

  it('拿不到声明总价时不显示这一段，也不拿明细合计冒充', () => {
    const card = buildBidCard({
      id: 'f1', filename: 'x.pdf', kind: 'bid', status: 'done', supplierName: '某供应商',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 89, total: 1000, pendingCount: 0 }, declaredTotal: null,
    })
    expect(card.statsText).toContain('明细合计')
    expect(card.statsText).not.toContain('声明总价')
  })
})

describe('req6：清单条目一律叫「项」，不叫「行」', () => {
  it('招标采购清单', () => {
    const card = buildTenderCard({
      filename: 'a.pdf', result: { tenderer: '某单位', row_count: 89 },
      summary: '', summaryLoading: false, progressPct: 100, stage: '', error: '',
    })
    expect(card.statsText).toBe('采购清单 89 项')
    expect(card.statsText).not.toContain('行')
  })

  it('Excel 采购清单', () => {
    const card = buildTenderListCard({ filename: 'l.xlsx', rowCount: 136, previewing: false, error: '' })
    expect(card.statsText).toBe('采购清单 136 项')
  })

  it('报价清单（含待确认计数）', () => {
    const card = buildBidCard({
      id: 'f1', filename: 'x.xlsx', kind: 'bid_list', status: 'done', supplierName: '某供应商',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 136, total: 500, pendingCount: 4 }, declaredTotal: null,
    })
    expect(card.statsText).toContain('报价清单 136 项')
    expect(card.pendingText).toContain('4 项待确认')
    expect(card.statsText + card.pendingText).not.toContain('行')
  })
})

describe('卡片可点开的条件', () => {
  it('识别完成的投标卡片可点开明细；还在跑的不行', () => {
    const base = {
      id: 'f1', filename: 'x.pdf', kind: 'bid' as const, supplierName: '某供应商',
      stage: '识别中', stageDetail: '3/8 页', progressPct: 40, error: '',
      summary: '', summaryLoading: false, declaredTotal: null,
    }
    expect(buildBidCard({ ...base, status: 'processing', stats: null }).detailKey).toBeNull()
    expect(buildBidCard({
      ...base, status: 'done', stats: { count: 1, total: 1, pendingCount: 0 },
    }).detailKey).toBe('f1')
  })

  it('识别失败的卡片显示错误、不显示进度条', () => {
    const card = buildBidCard({
      id: 'f1', filename: 'x.pdf', kind: 'bid', status: 'failed', supplierName: '',
      stage: '失败', stageDetail: '', progressPct: 30, error: '识别超时',
      summary: '', summaryLoading: false, stats: null, declaredTotal: null,
    })
    expect(card.errorText).toBe('识别超时')
    expect(card.progressPct).toBeNull()
  })
})

describe('§13：分类判据挂徽标，不进 toast', () => {
  it('判据原文原样带到 badgeTooltip 上，不被截断或丢弃', () => {
    const reason = "视觉分类前3页：封面明确标注'投标文件'，且包含具体公司名称"
    const card = buildBidCard({
      id: 'f1', filename: 'x.pdf', kind: 'bid', status: 'done', supplierName: '某供应商',
      stage: '', stageDetail: '', progressPct: 100, error: '',
      summary: '', summaryLoading: false,
      stats: { count: 1, total: 1, pendingCount: 0 }, declaredTotal: null,
      classifyReason: reason,
    })
    expect(card.badgeTooltip).toBe(reason)
  })

  it('没有判据时是空串，不是 undefined（模板 :title 会渲染出字面 undefined）', () => {
    const card = buildTenderCard({
      filename: 'a.pdf', result: { tenderer: '某单位', row_count: 1 },
      summary: '', summaryLoading: false, progressPct: 100, stage: '', error: '',
    })
    expect(card.badgeTooltip).toBe('')
  })
})


// ── design/29 §16：重试按钮只在"读不到状态"这类失败上出现 ──────────────
describe('retryKey', () => {
  const base = {
    id: 'f1', filename: 'x.pdf', kind: 'bid' as const,
    supplierName: '某供应商', stage: '', stageDetail: '', progressPct: 0,
    summary: '', summaryLoading: false, stats: null, declaredTotal: null,
  }

  it('连接中断 → 可重试（任务多半还在后台跑，重查一次就好）', () => {
    const c = buildBidCard({
      ...base, status: 'failed',
      error: '连接中断，读不到识别状态（识别可能仍在后台进行，可点重试）',
    })
    expect(c.retryKey).toBe('f1')
  })

  it('服务端明确判定的识别失败 → 不给重试按钮', () => {
    // 重试也是同样的结果。给一个点了没用的按钮比不给更糟。
    const c = buildBidCard({ ...base, status: 'failed', error: '识别失败：页面无可用文字层' })
    expect(c.retryKey).toBeNull()
  })

  it('正常完成的卡片没有重试入口', () => {
    const c = buildBidCard({
      ...base, status: 'done', error: '',
      stats: { count: 3, total: 100, pendingCount: 0 },
    })
    expect(c.retryKey).toBeNull()
  })
})
