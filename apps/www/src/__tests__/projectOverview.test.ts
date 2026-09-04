/**
 * 项目概述页（2026-09-03 排版重做）的契约测试。
 *
 * 为什么要有组件测试：开发库里所有项目都是空的（没有任何轮次/报价），
 * 有数据的那条分支——品类 tabs、流水线 Steps、当前轮报价表——在浏览器里
 * 根本走不到。类型检查只能保证字段名对，保证不了"两个金额分列显示"这类
 * 语义不变量。
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import Antd from 'ant-design-vue'
import { PIPELINE_STEPS, pipelineStep } from '../utils/pipeline'
import type { NextActionCode, ProjectOverviewResult } from '../api/client'

// ─── 流水线映射：错一格页面就会对项目状态说谎 ──────────────────────────────
describe('pipelineStep（next_action.code → Steps 当前步）', () => {
  it('每个 code 都有映射，且落在合法范围内', () => {
    const codes: NextActionCode[] = [
      'pending_upload', 'list_unconfirmed', 'pending_intake', 'ready_to_compare', 'basis_set',
    ]
    for (const code of codes) {
      const step = pipelineStep(code)
      expect(step).toBeGreaterThanOrEqual(0)
      expect(step).toBeLessThanOrEqual(PIPELINE_STEPS.length)
    }
  })

  it('流程越往后，步数单调不减', () => {
    // 这条顺序就是后端 derive_next_action 的分支顺序（design/45 §4.3 表）
    const order: NextActionCode[] = [
      'pending_upload', 'list_unconfirmed', 'pending_intake', 'ready_to_compare', 'basis_set',
    ]
    const steps = order.map(pipelineStep)
    for (let i = 1; i < steps.length; i += 1) {
      expect(steps[i]).toBeGreaterThanOrEqual(steps[i - 1])
    }
  })

  it('basis_set 越过末步 = 五步全部完成', () => {
    expect(pipelineStep('basis_set')).toBe(PIPELINE_STEPS.length)
  })

  it('未知 code 回落到第 0 步，而不是"已完成"', () => {
    // 后端加了新 code、前端还没跟上时，宁可显示"才刚开始"——显示"已完成"
    // 会让人以为可以定标了。
    expect(pipelineStep('some_future_code' as NextActionCode)).toBe(0)
  })

  it('未定标时不把最后一步标成完成', () => {
    expect(pipelineStep('ready_to_compare')).toBeLessThan(PIPELINE_STEPS.length)
  })
})

// ─── 页面渲染：有数据的分支 ────────────────────────────────────────────────
/** 用可变的**普通函数**做桩，不用 vi.fn()：vi.fn 会给返回的 promise 挂结果
 *  追踪，那个派生 promise 的拒绝没有 rejection handler，于是即使组件自己
 *  catch 干净了，vitest 仍会把它算成"未捕获拒绝"并判这条测试失败。 */
let overviewImpl: () => Promise<unknown> = () => Promise.resolve({ data: null })
vi.mock('../api', () => ({
  projectApi: { projectOverview: () => overviewImpl() },
  analysisApi: { bidMatrix: () => Promise.resolve({ data: null }) },
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { projectId: '42' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

function fixture(): ProjectOverviewResult {
  const submissions = [
    {
      submission_id: 1, supplier_id: 11, supplier_name: '甲供应商', round_id: 7,
      line_count: 31, detail_total: 128000, declared_total: 130000,
      submitted_at: '2026-09-01T10:00:00',
    },
    {
      // 文件封面没写总价：必须显示「文件未声明」，不能当成 0 去做减法
      submission_id: 2, supplier_id: 12, supplier_name: '乙供应商', round_id: 7,
      line_count: 29, detail_total: 119000, declared_total: null,
      submitted_at: '2026-09-01T11:00:00',
    },
  ]
  // stage 只有 pre_tender | formal（后端 models/quote_round.py STAGES 写入时校验）。
  // 早先这里写的是 'quote'，是个根本不存在的取值——测试因此抓到了 fixture 的错。
  const currentRound = {
    id: 7, seq: 2, name: '第二轮', stage: 'formal', status: 'open',
    is_final_basis: false, opened_at: '2026-09-01T09:00:00', closed_at: null, submissions,
  }
  const closedRound = {
    id: 6, seq: 1, name: '第一轮', stage: 'pre_tender', status: 'closed',
    is_final_basis: false, opened_at: '2026-08-01T09:00:00', closed_at: '2026-08-20T09:00:00',
    submissions: [submissions[0]],
    basis: { comparable: true, conflicts: [], unresolved: [] },
  }
  return {
    project: {
      id: 42, name: '测试项目', code: 'P2026-042', status: 'active',
      location: '上海', remark: null,
      created_at: '2026-08-20T14:32:00', created_by: '管理员',
    },
    pending_intake_count: 2,
    categories: [
      {
        category: '阀门',
        axis_kind: 'tender_anchor',
        list: {
          session_id: 3, confirmed: true, anchor_count: 31, version: 2,
          source_type: 'pdf', file_name: '招标清单.pdf',
          confirmed_at: '2026-08-30T12:00:00', confirmed_by: 'admin',
          brand_requirement: [{ brand_cn: '某品牌' }],
        },
        current_round: {
          id: 7, seq: 2, name: '第二轮', stage: 'formal', status: 'open', is_final_basis: false,
        },
        rounds: [currentRound, closedRound],
        suppliers: submissions,
        final_basis_round: null,
        has_confirmed_list: true,
        submission_count: 2,
        next_action: { code: 'ready_to_compare', label: '可出比价', count: null },
      },
      {
        category: '电缆',
        axis_kind: null,
        list: null,
        current_round: null,
        rounds: [],
        suppliers: [],
        final_basis_round: null,
        has_confirmed_list: false,
        submission_count: 0,
        next_action: { code: 'pending_upload', label: '待上传报价', count: null },
      },
    ],
  } as unknown as ProjectOverviewResult
}

async function mountView(data: ProjectOverviewResult) {
  overviewImpl = () => Promise.resolve({ data })
  const ProjectOverviewView = (await import('../views/compare/ProjectOverviewView.vue')).default
  const wrapper = mount(ProjectOverviewView, {
    global: { plugins: [Antd], stubs: { CategoryRecommendation: true } },
  })
  await new Promise((r) => setTimeout(r, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

describe('ProjectOverviewView（有数据）', () => {
  it('左侧品类导航每个品类一项，默认选中第一个', async () => {
    const w = await mountView(fixture())
    const items = w.findAll('.po__nav-item')
    expect(items.length).toBe(2)
    expect(items[0].text()).toContain('阀门')
    expect(items[1].text()).toContain('电缆')
    expect(items[0].classes()).toContain('po__nav-item--active')
  })

  it('点击导航切换右栏品类', async () => {
    const w = await mountView(fixture())
    await w.findAll('.po__nav-item')[1].trigger('click')
    await w.vm.$nextTick()
    expect(w.find('.po__cat-name').text()).toBe('电缆')
  })

  it('导航迷你进度点与右栏 Steps 用同一个映射', async () => {
    const w = await mountView(fixture())
    // 阀门 ready_to_compare → 4 步已完成；电缆 pending_upload → 0 步
    const dotsPerItem = w.findAll('.po__nav-item').map(
      (it) => it.findAll('.po__dot--done').length,
    )
    expect(dotsPerItem[0]).toBe(pipelineStep('ready_to_compare'))
    expect(dotsPerItem[1]).toBe(pipelineStep('pending_upload'))
  })

  it('轮次倒序、当前轮高亮，历史轮不摊开明细表', async () => {
    const w = await mountView(fixture())
    const rounds = w.findAll('.po__round')
    expect(rounds.length).toBe(2)
    // 第 2 轮（当前轮）在最上面
    expect(rounds[0].text()).toContain('第 2 轮')
    expect(rounds[0].classes()).toContain('po__round--current')
    expect(rounds[0].find('.po__round-table').exists()).toBe(true)
    expect(rounds[1].text()).toContain('第 1 轮')
    expect(rounds[1].find('.po__round-table').exists()).toBe(false)
  })

  it('轮次阶段与轮次名分开显示（stage 只有摸底/正式两个值）', async () => {
    const w = await mountView(fixture())
    const head = w.findAll('.po__round')[0].find('.po__round-head').text()
    expect(head).toContain('第二轮')     // 轮次名（自由文本）
    expect(head).toContain('正式报价')   // stage=formal 的标签
  })

  it('不做任何跨家金额聚合，也不出现"评标总价"字样', async () => {
    const w = await mountView(fixture())
    const text = w.text()
    // P0（口径维度设计 §4）：撤掉了"明细合计区间"。同一轮里一家可能「不含安装」、
    // 其余「含安装」，铜价基准也各不相同——给区间等于把不可比的数摆成可比的。
    expect(text).not.toContain('明细合计区间')
    // 评标口径要跑矩阵与三态门禁，概述页刻意不算。
    expect(text).not.toContain('评标总价')
    // 金额仍然逐家可见（当前轮的明细表）
    expect(text).toContain('明细合计')
  })

  it('给出口径可比性提示，而不是默默让用户去比', async () => {
    const w = await mountView(fixture())
    expect(w.find('.po__caution').exists()).toBe(true)
    expect(w.find('.po__caution').text()).toContain('交付范围')
  })

  it('历史轮也逐家列金额，不聚合', async () => {
    const w = await mountView(fixture())
    const closed = w.findAll('.po__round')[1]
    const rows = closed.findAll('.po__sub-row')
    expect(rows.length).toBe(1)          // fixture 的第 1 轮只有一家
    expect(rows[0].text()).toContain('甲供应商')
    expect(rows[0].text()).toContain('128,000')
  })

  it('建档时间与建档人来自后端字段', async () => {
    const w = await mountView(fixture())
    const text = w.text()
    expect(text).toContain('2026-08-20 14:32')
    expect(text).toContain('管理员')
  })

  it('右栏渲染五步流水线，且未定标时最后一步没走完', async () => {
    const w = await mountView(fixture())
    const steps = w.findAll('.po__steps .ant-steps-item')
    expect(steps.length).toBe(PIPELINE_STEPS.length)
    // ready_to_compare → 定标基准还没完成
    expect(steps[PIPELINE_STEPS.length - 1].classes()).not.toContain('ant-steps-item-finish')
  })

  it('项目级待校对份数出现在概览里', async () => {
    const w = await mountView(fixture())
    expect(w.find('.po__pending').text()).toBe('2')
  })

  it('明细合计与文件声明总价分列，缺声明时显示「文件未声明」而不是 0', async () => {
    const w = await mountView(fixture())
    const text = w.text()
    expect(text).toContain('明细合计')
    expect(text).toContain('文件声明总价')
    expect(text).toContain('文件未声明')
    // 缺失不能被当成 0：不允许出现拿 null 当 0 算出来的"差 119,000"
    expect(text).not.toContain('差 119,000')
  })

  it('待办文案直接用后端的 label，前端不改写', async () => {
    const w = await mountView(fixture())
    expect(w.text()).toContain('可出比价')
  })
})

describe('ProjectOverviewView（口径阻断卡）', () => {
  it('口径一致时不出阻断卡', async () => {
    const w = await mountView(fixture())
    expect(w.find('.po__block').exists()).toBe(false)
  })

  it('口径不一致时出阻断卡，并说清谁跟谁不同', async () => {
    // 母线第一轮的真实情形：一家「不含安装」，三家「含安装」
    const data = fixture()
    ;(data.categories[0] as any).rounds[0].basis = {
      comparable: false,
      conflicts: [{
        dim: 'delivery_scope',
        values: {
          '{"scope":"excl_installation"}': ['上海都安实业'],
          '{"scope":"incl_installation"}': ['江苏永旗电气', '上海塞克西德', '大航有能电气'],
        },
      }],
      unresolved: [],
    }
    const w = await mountView(data)
    const block = w.find('.po__block')
    expect(block.exists()).toBe(true)
    const text = block.text()
    expect(text).toContain('不可直接比较')
    expect(text).toContain('交付范围')      // 维度中文名
    expect(text).toContain('不含安装')      // 归一值可读化
    expect(text).toContain('上海都安实业')  // 说清是谁
    expect(text).toContain('江苏永旗电气')
  })

  it('口径未确认时说明"未确认不等于一致"', async () => {
    const data = fixture()
    ;(data.categories[0] as any).rounds[0].basis = {
      comparable: false, conflicts: [],
      unresolved: [{ submission_id: 1, supplier_name: '甲供应商' }],
    }
    const w = await mountView(data)
    const text = w.find('.po__block').text()
    expect(text).toContain('口径待确认')
    expect(text).toContain('甲供应商')
    expect(text).toContain('未确认不等于一致')
  })

  it('原文未声明是一个取值，不显示成内部键名', async () => {
    const data = fixture()
    ;(data.categories[0] as any).rounds[0].basis = {
      comparable: false,
      conflicts: [{
        dim: 'delivery_scope',
        values: {
          '{"scope":"excl_installation"}': ['甲'],
          '__not_declared__': ['乙'],
        },
      }],
      unresolved: [],
    }
    const w = await mountView(data)
    const text = w.find('.po__block').text()
    expect(text).toContain('原文未声明')
    expect(text).not.toContain('__not_declared__')
  })
})

describe('ProjectOverviewView（加载失败）', () => {
  it('请求失败时给出错误页和重试，而不是白屏', async () => {
    overviewImpl = () => Promise.reject({ response: { data: { detail: '项目不存在' } } })
    const ProjectOverviewView = (await import('../views/compare/ProjectOverviewView.vue')).default
    const w = mount(ProjectOverviewView, {
      global: { plugins: [Antd], stubs: { CategoryRecommendation: true } },
    })
    await new Promise((r) => setTimeout(r, 0))
    await w.vm.$nextTick()
    expect(w.text()).toContain('项目概述加载失败')
    expect(w.text()).toContain('重 试')
  })
})
