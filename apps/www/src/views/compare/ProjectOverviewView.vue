<!--
  design/45 §5 —— 项目概述（/workspace/:projectId 的落地页）。

  三条结构性约束，改动前先读 design/45 §3：

  1. **只读**。所有写操作链去既有页面（工作台 / 对齐核查 / 轮次栏）。
     WorkspaceView.vue 已经 2000+ 行，概述页一旦长出写操作，一个版本内就会
     变成第二个。
  2. **不推荐中标人**（约束 C1）。`get_evaluation_policy` 对所有项目返回
     UNKNOWN，评标办法必须来自招标文件。概述页比矩阵页更容易被读成结论，
     这条在这里绑得更紧，不是更松。
  3. **每品类分域**（D-1 / 约束 C2）。轮次、行轴、矩阵全部按
     (project, category) 分域，印一个项目级轮次号就是对跨品类项目说谎。

  2026-09-03 版面（用户按原型评审第三档定案）：**左侧品类导航 + 右侧轮次卡
  列表**。上一版的品类 tabs 在品类多到 6+ 个时会挤；轮次本来就是时间序，
  藏在「当前轮/历史轮」子 tab 后面等于把该一眼看全的东西折起来。

  **原型里被砍掉的字段，理由记在这里，免得下一轮又画回来**：
  - 「评标总价区间（含税）」——概述端点**刻意不算矩阵**（routes/projects.py
    有原话）。评标总价要跑 import_and_match + 三态门禁 + 锚点轴校验，放进概述
    就是每品类每轮各跑一次矩阵，还会长出第二套"大致谁便宜"的说法。
  - 「明细合计区间」——P0 撤掉（口径维度设计 §4）。真实材料里同一轮可能一家
    「不含安装」其余「含安装」、铜价基准还各不相同，给区间等于把不可比的数摆成
    一个可比的区间。金额只**逐家列**，跨家聚合一律不给。
  - 「每轮待校对 N 项」——待校对只到**项目粒度**（job 上没有可靠 category），
    单位是**份**（文件）不是**项**（清单行）。所以它只出现在页眉。
  - 「清单 v2.1」——`version` 是整数，没有小版本号。
  - 「需报价/已报价/已校对项数」——没有任何服务提供行级覆盖统计。
  - 「最近活动」描述文字——概述端点只有时间戳，没有活动流。

  命名：本页叫 ProjectOverview。WorkspaceView.vue 里的 `viewMode:
  'overview'|'detail'` 是另一回事（文件卡片总览 vs 单文件明细），不要复用。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  ArrowLeftOutlined, RightOutlined, FileTextOutlined, TeamOutlined,
  InfoCircleOutlined, WarningOutlined,
} from '@ant-design/icons-vue'
import { projectApi } from '@/api'
import type {
  NextActionCode, OverviewRound, ProjectOverviewCategory, ProjectOverviewResult,
  QuoteRoundStage,
} from '@/api/client'
import { formatMoney } from '@/utils/docCards'
// 流水线映射是纯展示表，抽在 utils 里单测（当前步只由后端 next_action.code 决定）
import { PIPELINE_STEPS, pipelineStep } from '@/utils/pipeline'
import CategoryRecommendation from './components/CategoryRecommendation.vue'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => Number(route.params.projectId))

const data = ref<ProjectOverviewResult | null>(null)
const loading = ref(false)
/** 加载失败与「加载中」必须分开：两种情况 data 都是 null，没有独立分支时
 *  失败是一整片白屏，看不出是没数据还是请求挂了。 */
const loadError = ref<string | null>(null)
const activeCategory = ref<string>('')

async function fetchData() {
  loading.value = true
  loadError.value = null
  try {
    const resp = await projectApi.projectOverview(projectId.value)
    data.value = resp.data
  } catch (e: any) {
    const detail = e?.response?.data?.detail || '项目概述加载失败'
    data.value = null
    loadError.value = detail
    message.error(detail)
  } finally {
    loading.value = false
  }
}
onMounted(fetchData)

// 选中品类跟着数据走：刷新后品类集合可能变（新确认一份清单就多一个品类），
// 选中项若指向已消失的品类，右栏会整片空白。
watch(data, (d) => {
  const cats = d?.categories.map(c => c.category) ?? []
  if (!cats.length) {
    activeCategory.value = ''
    return
  }
  if (!cats.includes(activeCategory.value)) activeCategory.value = cats[0]
})

const currentCategory = computed<ProjectOverviewCategory | null>(
  () => data.value?.categories.find(c => c.category === activeCategory.value) ?? null,
)

// 标签颜色跟入口列表保持一致；文案一律用后端给的 label，前端不拼
// （两个入口必须说同一句话）。
const NEXT_ACTION_COLOR: Record<NextActionCode, string> = {
  pending_upload: 'default',
  list_unconfirmed: 'orange',
  pending_intake: 'blue',
  ready_to_compare: 'green',
  basis_set: 'gold',
}
function nextActionColor(code: NextActionCode) {
  return NEXT_ACTION_COLOR[code] ?? 'default'
}

/** 轮次**阶段**只有两个值（`QuoteRoundStage = 'pre_tender' | 'formal'`）。
 *  轮次名（"最终澄清报价"之类）是自由文本，跟阶段是两回事——原型曾把两者混成
 *  一列，会让人以为系统能按"最终澄清"这个阶段做门禁。这里分开显示。 */
const STAGE_LABEL: Record<QuoteRoundStage, string> = {
  pre_tender: '招标前摸底',
  formal: '正式报价',
}
function stageLabel(stage: QuoteRoundStage) {
  return STAGE_LABEL[stage] ?? stage
}

function fmtDate(d: string | null) {
  return d ? d.replace('T', ' ').slice(0, 16) : '—'
}

/** 口径维度的中文名。纯展示映射——**判定在后端**
 *  （services/matrix/basis_consistency.py），前端不自算可比性。 */
const BASIS_DIM_LABEL: Record<string, string> = {
  delivery_scope: '交付范围',
  commodity_benchmark: '原材料价格基准',
  payment_terms: '付款条件',
}
function basisDimLabel(dim: string) {
  return BASIS_DIM_LABEL[dim] ?? dim
}

/** 归一值的可读文案。`__not_declared__` 是一个**取值**（原文里没声明），
 *  不是缺失——它跟"明说不含安装"冲突，正是要拦的情况。 */
function basisValueLabel(key: string) {
  if (key === '__not_declared__') return '原文未声明'
  try {
    const v = JSON.parse(key)
    if (v?.scope === 'incl_installation') return '含安装'
    if (v?.scope === 'excl_installation') return '不含安装'
    if (v?.scope === 'other') return '其他口径（见原文）'
    if (v?.material) return `${v.material} ${v.price}${v.unit || ''}`
    if (v?.terms_text) return String(v.terms_text).slice(0, 40)
    return key
  } catch {
    return key
  }
}

/** 明细合计与文件声明总价的差额。两个数都在时才算——缺一个就没有"差"可言，
 *  返回 null 让模板显示「文件未声明」而不是把缺失当成 0 去做减法。 */
function totalsDelta(detail: number, declared: number | null): number | null {
  return declared == null ? null : detail - declared
}

/** 轮次倒序：最新的在最上面。轮次是时间序，最该看的是当前轮。 */
const roundsOf = (cat: ProjectOverviewCategory): OverviewRound[] =>
  [...cat.rounds].sort((a, b) => b.seq - a.seq)

const isCurrentRound = (cat: ProjectOverviewCategory, r: OverviewRound) =>
  r.id === cat.current_round?.id

const SUBMISSION_COLUMNS = [
  { title: '供应商', dataIndex: 'supplier_name', key: 'name' },
  { title: '行数', dataIndex: 'line_count', key: 'lines', width: 80 },
  { title: '明细合计', key: 'detail', width: 150 },
  { title: '文件声明总价', key: 'declared', width: 180 },
  { title: '入库时间', key: 'at', width: 150 },
]

const stepItems = computed(() => PIPELINE_STEPS.map(t => ({ title: t })))

function goList() {
  router.push('/workspace')
}
/** 页头那个按钮保持项目级中性入口（不带轮次语义）。品类级动作走
 *  `goCategoryWork()`——轮次是 (project, category) 维度的，不带品类进去，
 *  工作台的轮次栏根本不显示。 */
function goCompare() {
  router.push(`/workspace/${projectId.value}/compare`)
}

/** 带着品类进工作台，可选直接唤起「开启新一轮」弹窗。
 *
 *  **不在概述页复制一套轮次管理 UI**：开新轮的后果说明（"当前轮次将关闭…"）
 *  只在工作台那一个弹窗里写着，复制第二份迟早两处说两套话。 */
function goCategoryWork(category: string, opts: { newRound?: boolean } = {}) {
  router.push({
    path: `/workspace/${projectId.value}/compare`,
    query: opts.newRound ? { category, newRound: '1' } : { category },
  })
}
function goAlign(category: string) {
  router.push({ path: `/workspace/${projectId.value}/align`, query: { category } })
}
</script>

<template>
  <div class="po">
    <!-- 加载中 / 加载失败 / 有数据，三个分支互斥 -->
    <a-skeleton v-if="loading && !data" active :paragraph="{ rows: 6 }" />

    <a-result
      v-else-if="loadError"
      status="warning"
      title="项目概述加载失败"
      :sub-title="loadError"
    >
      <template #extra>
        <a-space>
          <a-button @click="goList">返回项目列表</a-button>
          <a-button type="primary" @click="fetchData">重 试</a-button>
        </a-space>
      </template>
    </a-result>

    <a-spin v-else-if="data" :spinning="loading">
      <!-- ── A 页头 ──────────────────────────────────────────────────── -->
      <div class="po__header">
        <div class="po__header-main">
          <a-button type="text" class="po__back" @click="goList">
            <ArrowLeftOutlined />
          </a-button>
          <h1 class="po__title">
            {{ data.project.name }}
            <a-tag :color="data.project.status === 'active' ? 'processing' : 'default'">
              {{ data.project.status }}
            </a-tag>
          </h1>
        </div>
        <a-space>
          <a-button @click="fetchData">刷 新</a-button>
          <a-button type="primary" @click="goCompare">
            进入比价工作台 <RightOutlined />
          </a-button>
        </a-space>
      </div>

      <!-- ── B 项目级信息 ────────────────────────────────────────────── -->
      <a-card class="po__card" :body-style="{ padding: '16px 20px' }">
        <a-descriptions :column="4" size="small">
          <a-descriptions-item label="项目编号">
            <span v-if="data.project.code">{{ data.project.code }}</span>
            <span v-else class="po__muted">未识别</span>
          </a-descriptions-item>
          <a-descriptions-item label="项目地点">
            <span v-if="data.project.location">{{ data.project.location }}</span>
            <span v-else class="po__muted">—</span>
          </a-descriptions-item>
          <a-descriptions-item label="品类数">{{ data.categories.length }} 个</a-descriptions-item>
          <a-descriptions-item label="待校对入库">
            <!-- 项目粒度、单位是**份**（文件），不是清单行。不摊到品类里：
                 job 上没有可靠 category，硬分会让数字在识别完成后自己跳动。 -->
            <template v-if="data.pending_intake_count > 0">
              <b class="po__pending">{{ data.pending_intake_count }}</b> 份
              <a-button type="link" size="small" @click="goCompare">去校对 →</a-button>
            </template>
            <span v-else class="po__muted">无</span>
          </a-descriptions-item>
          <a-descriptions-item label="建档时间">
            {{ fmtDate(data.project.created_at) }}
          </a-descriptions-item>
          <a-descriptions-item label="建档人">
            <span v-if="data.project.created_by">{{ data.project.created_by }}</span>
            <span v-else class="po__muted">—</span>
          </a-descriptions-item>
          <a-descriptions-item label="备注" :span="2">
            <span v-if="data.project.remark">{{ data.project.remark }}</span>
            <span v-else class="po__muted">—</span>
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <a-card v-if="data.categories.length === 0" class="po__card">
        <a-empty description="还没有采购清单或报价——拖入第一份文件即可开始">
          <a-button type="primary" @click="goCompare">去上传</a-button>
        </a-empty>
      </a-card>

      <!-- ── C 左品类导航 + 右轮次卡 ─────────────────────────────────── -->
      <div v-else class="po__body">
        <!-- 左：品类导航。每张卡自带迷你进度点，多品类可以竖着扫。 -->
        <a-card class="po__nav" :body-style="{ padding: '12px' }">
          <div class="po__nav-title">品类导航</div>
          <button
            v-for="cat in data.categories" :key="cat.category"
            type="button"
            class="po__nav-item"
            :class="{ 'po__nav-item--active': cat.category === activeCategory }"
            @click="activeCategory = cat.category"
          >
            <div class="po__nav-head">
              <span class="po__nav-name">{{ cat.category }}</span>
              <span class="po__nav-round">
                {{ cat.current_round ? `第 ${cat.current_round.seq} 轮` : '尚未开轮' }}
              </span>
            </div>
            <div class="po__nav-action">{{ cat.next_action.label }}</div>
            <!-- 迷你进度点与右栏 Steps 同一个映射，不会出现两套说法 -->
            <div class="po__dots">
              <span
                v-for="(s, i) in PIPELINE_STEPS" :key="s"
                class="po__dot"
                :class="{ 'po__dot--done': i < pipelineStep(cat.next_action.code) }"
                :title="s"
              />
            </div>
          </button>
        </a-card>

        <!-- 右：所选品类 -->
        <div class="po__main">
          <a-card v-if="currentCategory" class="po__card" :body-style="{ padding: '16px 20px' }">
            <div class="po__cat-head">
              <div class="po__cat-title">
                <span class="po__cat-name">{{ currentCategory.category }}</span>
                <a-tag v-if="currentCategory.final_basis_round" color="gold">
                  定标基准：第{{ currentCategory.final_basis_round.seq }}轮
                </a-tag>
              </div>
              <a-space>
                <a-tag :color="nextActionColor(currentCategory.next_action.code)">
                  {{ currentCategory.next_action.label }}
                  <template v-if="currentCategory.next_action.count != null">
                    {{ currentCategory.next_action.count }} 份
                  </template>
                </a-tag>
              </a-space>
            </div>

            <!-- 品类级入口（2026-09-04）：按钮**说出目标轮次**。
                 原来只有页头一个不带品类的「进入比价工作台」，进去后轮次栏
                 不显示，用户无从知道这次上传归到哪一轮。 -->
            <div class="po__cat-actions">
              <template v-if="currentCategory.current_round">
                <a-button type="primary" @click="goCategoryWork(currentCategory.category)">
                  上传报价到「第 {{ currentCategory.current_round.seq }} 轮」
                  <RightOutlined />
                </a-button>
                <a-button @click="goCategoryWork(currentCategory.category, { newRound: true })">
                  开启新一轮…
                </a-button>
                <span class="po__muted">
                  开启新一轮会关闭第 {{ currentCategory.current_round.seq }} 轮，此后上传的报价归入新一轮
                </span>
              </template>
              <template v-else>
                <a-button type="primary" @click="goCategoryWork(currentCategory.category)">
                  上传报价 <RightOutlined />
                </a-button>
                <span class="po__muted">首轮将在首次确认报价时自动开启</span>
              </template>
            </div>

            <a-steps
              :current="pipelineStep(currentCategory.next_action.code)"
              size="small"
              class="po__steps"
              :items="stepItems"
            />

            <!-- 采购清单 -->
            <div class="po__section">
              <div class="po__section-title"><FileTextOutlined /> 采购清单</div>
              <a-descriptions v-if="currentCategory.list" :column="3" size="small">
                <a-descriptions-item label="清单项数">
                  <b>{{ currentCategory.list.anchor_count }}</b> 项
                </a-descriptions-item>
                <a-descriptions-item label="版本">
                  第 {{ currentCategory.list.version }} 版
                </a-descriptions-item>
                <a-descriptions-item label="来源">
                  {{ currentCategory.list.source_type === 'pdf' ? '招标 PDF' : 'Excel' }}
                </a-descriptions-item>
                <a-descriptions-item label="确认时间">
                  {{ fmtDate(currentCategory.list.confirmed_at) }}
                </a-descriptions-item>
                <a-descriptions-item label="来源文件" :span="2">
                  <span v-if="currentCategory.list.file_name">
                    {{ currentCategory.list.file_name }}
                  </span>
                  <span v-else class="po__muted">—</span>
                </a-descriptions-item>
                <a-descriptions-item
                  v-if="currentCategory.list.brand_requirement.length"
                  label="品牌要求" :span="3"
                >
                  <a-tag v-for="(b, i) in currentCategory.list.brand_requirement" :key="i">
                    {{ b.brand_cn || b.brand_en }}
                  </a-tag>
                </a-descriptions-item>
              </a-descriptions>
              <div v-else class="po__muted">
                还没有已确认的采购清单——没有清单就没有招标侧真值，本品类只能做预览比价。
                <a-button type="link" size="small" @click="goCompare">去确认清单 →</a-button>
              </div>
            </div>
          </a-card>

          <!-- 轮次卡列表：倒序，当前轮高亮。轮次是时间序，一眼看全，不折叠。 -->
          <a-card
            v-if="currentCategory"
            class="po__card"
            :body-style="{ padding: '16px 20px' }"
          >
            <div class="po__section-title"><TeamOutlined /> 轮次概览</div>

            <div v-if="!roundsOf(currentCategory).length" class="po__muted po__pane-empty">
              本品类尚未开轮——首轮将在首次确认报价时自动开启。
              <a-button type="link" size="small" @click="goCompare">去上传报价 →</a-button>
            </div>

            <div
              v-for="r in roundsOf(currentCategory)" :key="r.id"
              class="po__round"
              :class="{ 'po__round--current': isCurrentRound(currentCategory, r) }"
            >
              <div class="po__round-head">
                <div class="po__round-id">
                  <a-tag v-if="isCurrentRound(currentCategory, r)" color="blue">当前轮</a-tag>
                  <span class="po__round-seq">第 {{ r.seq }} 轮</span>
                  <span class="po__round-name">{{ r.name }}</span>
                </div>
                <a-space :size="4">
                  <!-- 阶段与轮次名分开：stage 只有 摸底/正式 两个值 -->
                  <a-tag>{{ stageLabel(r.stage) }}</a-tag>
                  <a-badge
                    :status="r.status === 'open' ? 'processing' : 'default'"
                    :text="r.status === 'open' ? '收集中' : '已关闭'"
                  />
                  <a-tag v-if="r.is_final_basis" color="gold">定标基准</a-tag>
                </a-space>
              </div>

              <div class="po__round-meta">
                <span v-if="r.submissions.length">
                  已入库报价 <b>{{ r.submissions.length }}</b> 家
                </span>
                <span v-else class="po__muted">本轮还没有已入库的报价</span>
                <a-divider type="vertical" />
                <span class="po__muted">
                  {{ r.status === 'open'
                    ? `开启于 ${fmtDate(r.opened_at)}`
                    : `关闭于 ${fmtDate(r.closed_at)}` }}
                </span>
              </div>

              <!-- 口径阻断卡（P1）：comparable=false 时说清"为什么这一轮不能直接比"。
                   判定全在后端；这里只渲染，不自算。 -->
              <div v-if="r.basis && !r.basis.comparable" class="po__block">
                <div class="po__block-title">
                  <WarningOutlined /> 本轮口径不一致，总价不可直接比较
                </div>
                <div v-for="c in r.basis.conflicts" :key="c.dim" class="po__block-dim">
                  <span class="po__block-dimname">{{ basisDimLabel(c.dim) }}</span>
                  <span
                    v-for="(names, key) in c.values" :key="key"
                    class="po__block-group"
                  >
                    <b>{{ basisValueLabel(String(key)) }}</b>：{{ names.join('、') }}
                  </span>
                </div>
                <div v-if="r.basis.unresolved.length" class="po__block-dim">
                  <span class="po__block-dimname">口径待确认</span>
                  <span class="po__block-group">
                    {{ r.basis.unresolved.map(u => u.supplier_name).join('、') }}
                    <span class="po__muted">——未确认不等于一致，先确认再比</span>
                  </span>
                </div>
              </div>

              <!-- 当前轮直接摊开供应商明细；历史轮只给报价清单，不给结论（D-2） -->
              <a-table
                v-if="isCurrentRound(currentCategory, r) && r.submissions.length"
                size="small" :pagination="false" row-key="submission_id"
                class="po__round-table"
                :data-source="r.submissions"
                :columns="SUBMISSION_COLUMNS"
              >
                <template #bodyCell="{ column, record }">
                  <!-- 两个总价分列显示，永不合并（FUNCTIONAL §5） -->
                  <template v-if="column.key === 'detail'">
                    {{ formatMoney(record.detail_total) }}
                  </template>
                  <template v-else-if="column.key === 'declared'">
                    <span v-if="record.declared_total == null" class="po__muted">文件未声明</span>
                    <template v-else>
                      {{ formatMoney(record.declared_total) }}
                      <a-tag
                        v-if="totalsDelta(record.detail_total, record.declared_total)"
                        color="orange" class="po__delta"
                      >
                        差 {{ formatMoney(totalsDelta(record.detail_total, record.declared_total)!) }}
                      </a-tag>
                    </template>
                  </template>
                  <template v-else-if="column.key === 'at'">
                    {{ fmtDate(record.submitted_at) }}
                  </template>
                </template>
              </a-table>

              <!-- 历史轮同样**逐家列示**，不做区间/排序/聚合：口径是否可比
                   系统还判不了（见下方提示），任何聚合都是在替用户下结论。 -->
              <div v-else-if="r.submissions.length" class="po__round-suppliers">
                <div v-for="sub in r.submissions" :key="sub.submission_id" class="po__sub-row">
                  <span class="po__sub-name">{{ sub.supplier_name }}</span>
                  <span class="po__sub-amount">{{ formatMoney(sub.detail_total) }}</span>
                </div>
              </div>
            </div>

            <!-- P0（口径维度设计 §4）：撤掉了"明细合计区间"。金额只逐家列，
                 不给任何跨家聚合——真实材料里同一轮的总价可能一家「不含安装」、
                 其余「含安装」，铜价基准也各不相同，聚合等于把不可比的数摆成
                 一个可比的区间。系统能识别这些口径之前，宁可不给。 -->
            <div v-if="roundsOf(currentCategory).some(r => r.submissions.length > 1)"
                 class="po__caution">
              <InfoCircleOutlined />
              <span>
                各家总价是否可比，取决于<b>交付范围</b>（含/不含安装）、<b>原材料价格基准</b>（如铜价）、<b>付款条件</b>等口径。系统目前尚未识别这些口径——横向比较前请先核对各家报价文件的备注与商务条款。
              </span>
            </div>

            <div v-if="roundsOf(currentCategory).length > 1" class="po__hint">
              历史轮次只列报价清单。当时的结论不做留存展示——重算会随清单改版、
              供应商合并而漂移，显示一个会变的"当时结论"比不显示更糟。
            </div>
          </a-card>

          <!-- 比价建议：懒加载，只对锚点轴出正式结论 -->
          <CategoryRecommendation
            v-if="currentCategory"
            :project-id="projectId"
            :category="currentCategory.category"
            :axis-kind="currentCategory.axis_kind"
            :submission-count="currentCategory.submission_count"
            @go-align="goAlign(currentCategory.category)"
          />
        </div>
      </div>
    </a-spin>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.po {
  padding: 16px 24px;

  &__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 12px;
  }

  &__header-main { display: flex; align-items: center; gap: 4px; min-width: 0; }

  &__back {
    color: @text-color-secondary;

    &:hover { color: @primary-color; }
  }

  &__title {
    margin: 0; font-size: 20px; font-weight: 600; color: @heading-color;
    display: flex; align-items: center; gap: 10px;
  }

  &__muted { color: @text-color-secondary; font-size: 12px; }

  &__pending { color: @alert-red-color; font-size: 15px; }

  &__card { margin-bottom: 16px; }

  // ── 左右两栏：左品类导航固定宽，右内容自适应 ──────────────────────
  &__body {
    display: flex;
    align-items: flex-start;
    gap: 16px;
  }

  &__nav {
    width: 240px;
    flex: 0 0 240px;
    position: sticky;
    top: 16px;
  }

  &__main { flex: 1; min-width: 0; }

  &__nav-title {
    font-size: 13px; font-weight: 600; color: @heading-color;
    padding: 4px 8px 10px;
  }

  &__nav-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 10px 12px;
    margin-bottom: 8px;
    border: 1px solid @border-color-base;
    border-radius: @border-radius-base;
    background: @component-background;
    cursor: pointer;
    transition: all 0.2s;

    &:hover { border-color: @primary-color; }

    &--active {
      border-color: @primary-color;
      background: @primary-1;
    }

    &:last-child { margin-bottom: 0; }
  }

  &__nav-head {
    display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  }

  &__nav-name { font-size: 14px; font-weight: 600; color: @heading-color; }

  &__nav-round { font-size: 12px; color: @text-color-secondary; }

  &__nav-action { margin-top: 4px; font-size: 12px; color: @text-color-secondary; }

  &__dots { display: flex; align-items: center; gap: 6px; margin-top: 8px; }

  &__dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: @border-color-base;

    &--done { background: @primary-color; }
  }

  // ── 右栏 ──────────────────────────────────────────────────────────
  &__cat-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
  }

  &__cat-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

  &__cat-name { font-size: 16px; font-weight: 600; color: @heading-color; }

  &__cat-actions {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    margin-top: 14px;
  }

  &__steps { margin: 20px 0 4px; max-width: 720px; }

  &__section {
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px solid @border-color-split;
  }

  &__section-title {
    font-size: 13px; font-weight: 600; margin-bottom: 10px;
    display: flex; align-items: center; gap: 6px;
  }

  &__pane-empty { padding: 8px 0; }

  // ── 轮次卡 ────────────────────────────────────────────────────────
  &__round {
    border: 1px solid @border-color-base;
    border-radius: @border-radius-base;
    padding: 12px 14px;
    margin-bottom: 12px;

    &--current {
      border-color: @primary-color;
      background: fade(@primary-1, 40%);
    }

    &:last-of-type { margin-bottom: 0; }
  }

  &__round-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
  }

  &__round-id { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }

  &__round-seq { font-size: 15px; font-weight: 600; color: @heading-color; }

  &__round-name { font-size: 13px; color: @text-color-secondary; }

  &__round-meta {
    margin-top: 8px; font-size: 12px; color: @text-color;
    display: flex; align-items: center; flex-wrap: wrap;
  }

  &__caution {
    display: flex; align-items: flex-start; gap: 8px;
    margin-top: 12px; padding: 10px 12px;
    border-radius: @border-radius-base;
    background: #fffbe6;
    border: 1px solid #ffe58f;
    font-size: 12px;
    color: @text-color;
    line-height: 1.7;
  }

  &__sub-row {
    display: flex; align-items: baseline; gap: 12px;
    padding: 3px 0; font-size: 12px;
  }

  &__sub-name { color: @text-color; }

  &__sub-amount { color: @text-color-secondary; }

  &__block {
    margin-top: 10px;
    padding: 10px 12px;
    border-radius: @border-radius-base;
    background: #fff2f0;
    border: 1px solid #ffccc7;
    font-size: 12px;
    line-height: 1.8;
  }

  &__block-title {
    font-weight: 600;
    color: @alert-red-color;
    display: flex; align-items: center; gap: 6px;
    margin-bottom: 4px;
  }

  &__block-dim { display: flex; flex-wrap: wrap; gap: 4px 14px; }

  &__block-dimname {
    min-width: 96px;
    color: @text-color-secondary;
  }

  &__block-group { color: @text-color; }

  &__round-table { margin-top: 12px; }

  &__round-suppliers { margin-top: 8px; }

  &__delta { margin-left: 6px; }

  &__hint {
    margin-top: 12px; font-size: 12px; color: @text-color-secondary; opacity: 0.8;
  }
}
</style>
