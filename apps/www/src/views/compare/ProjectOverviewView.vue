<!--
  design/45 §5 —— 项目概述（/workspace/:projectId 的落地页）。

  三条结构性约束，改动前先读 design/45 §3：

  1. **只读**。所有写操作链去既有页面（工作台 / 对齐核查 / 轮次栏）。
     WorkspaceView.vue 已经 2000+ 行，概述页一旦长出写操作，一个版本内就会
     变成第二个。
  2. **不推荐中标人**（约束 C1）。`get_evaluation_policy` 对所有项目返回
     UNKNOWN，评标办法必须来自招标文件。概述页比矩阵页更容易被读成结论，
     这条在这里绑得更紧，不是更松。
  3. **每品类一张卡**（D-1 / 约束 C2）。轮次、行轴、矩阵全部按
     (project, category) 分域，印一个项目级轮次号就是对跨品类项目说谎。

  命名：本页叫 ProjectOverview。WorkspaceView.vue 里的 `viewMode:
  'overview'|'detail'` 是另一回事（文件卡片总览 vs 单文件明细），不要复用。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { RightOutlined, FileTextOutlined, TeamOutlined } from '@ant-design/icons-vue'
import { projectApi } from '@/api'
import type {
  NextActionCode, OverviewRound, ProjectOverviewCategory, ProjectOverviewResult,
} from '@/api/client'
import { formatMoney } from '@/utils/docCards'
import CategoryRecommendation from './components/CategoryRecommendation.vue'

const route = useRoute()
const router = useRouter()
const projectId = computed(() => Number(route.params.projectId))

const data = ref<ProjectOverviewResult | null>(null)
const loading = ref(false)

async function fetchData() {
  loading.value = true
  try {
    const resp = await projectApi.projectOverview(projectId.value)
    data.value = resp.data
  } catch (e: any) {
    data.value = null
    message.error(e?.response?.data?.detail || '项目概述加载失败')
  } finally {
    loading.value = false
  }
}
onMounted(fetchData)

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

function fmtDate(d: string | null) {
  return d ? d.replace('T', ' ').slice(0, 16) : '—'
}

/** 明细合计与文件声明总价的差额。两个数都在时才算——缺一个就没有"差"可言，
 *  返回 null 让模板显示「文件未声明」而不是把缺失当成 0 去做减法。 */
function totalsDelta(detail: number, declared: number | null): number | null {
  return declared == null ? null : detail - declared
}

const closedRounds = (cat: ProjectOverviewCategory): OverviewRound[] =>
  cat.rounds.filter(r => r.id !== cat.current_round?.id)

const currentRoundOf = (cat: ProjectOverviewCategory): OverviewRound | null =>
  cat.rounds.find(r => r.id === cat.current_round?.id) ?? null

function goCompare() {
  router.push(`/workspace/${projectId.value}/compare`)
}
function goAlign(category: string) {
  router.push({ path: `/workspace/${projectId.value}/align`, query: { category } })
}
</script>

<template>
  <div class="po">
    <a-spin :spinning="loading">
      <template v-if="data">
        <!-- ── A 采购概述 ────────────────────────────────────────────── -->
        <div class="po__header">
          <div>
            <h1 class="po__title">{{ data.project.name }}</h1>
            <div class="po__sub">
              <span v-if="data.project.code">编号 {{ data.project.code }}</span>
              <span v-else class="po__muted">编号未识别</span>
              <a-divider type="vertical" />
              <span>{{ data.categories.length }} 个品类</span>
              <a-divider type="vertical" />
              <span>{{ data.project.status }}</span>
            </div>
          </div>
          <a-space>
            <a-button @click="fetchData">刷新</a-button>
            <a-button type="primary" @click="goCompare">
              进入比价工作台 <RightOutlined />
            </a-button>
          </a-space>
        </div>

        <!-- ── 顶部状态条（§5.3）：每一段都是入口，不是纯文字 ─────────── -->
        <a-alert v-if="data.pending_intake_count > 0" type="info" show-icon banner
                 class="po__strip">
          <template #message>
            有 {{ data.pending_intake_count }} 份报价已识别、尚未校对入库
            <a-button type="link" size="small" @click="goCompare">去校对 →</a-button>
          </template>
        </a-alert>

        <a-empty v-if="data.categories.length === 0"
                 description="还没有采购清单或报价——拖入第一份文件即可开始">
          <a-button type="primary" @click="goCompare">去上传</a-button>
        </a-empty>

        <!-- ── 每品类一张状态卡（D-1）────────────────────────────────── -->
        <a-card v-for="cat in data.categories" :key="cat.category" class="po__cat">
          <div class="po__cat-head">
            <div class="po__cat-title">
              <a-tag color="blue">{{ cat.category }}</a-tag>
              <span v-if="cat.current_round" class="po__cat-round">
                第{{ cat.current_round.seq }}轮
                <a-badge
                  :status="cat.current_round.status === 'open' ? 'processing' : 'default'"
                  :text="cat.current_round.status === 'open' ? '收集中' : '已关闭'"
                />
              </span>
              <span v-else class="po__muted">尚未开轮</span>
              <a-tag v-if="cat.final_basis_round" color="gold">
                定标基准：第{{ cat.final_basis_round.seq }}轮
              </a-tag>
            </div>
            <a-tag :color="nextActionColor(cat.next_action.code)">
              {{ cat.next_action.label }}
              <template v-if="cat.next_action.count != null">
                {{ cat.next_action.count }} 份
              </template>
            </a-tag>
          </div>

          <!-- ── B 采购清单 ─────────────────────────────────────────── -->
          <div class="po__section">
            <div class="po__section-title"><FileTextOutlined /> 采购清单</div>
            <div v-if="cat.list" class="po__list-meta">
              <span><b>{{ cat.list.anchor_count }}</b> 项</span>
              <a-divider type="vertical" />
              <span>第 {{ cat.list.version }} 版</span>
              <a-divider type="vertical" />
              <span>来源 {{ cat.list.source_type === 'pdf' ? '招标 PDF' : 'Excel' }}</span>
              <a-divider type="vertical" />
              <span>确认于 {{ fmtDate(cat.list.confirmed_at) }}</span>
              <span v-if="cat.list.file_name" class="po__muted po__file">
                {{ cat.list.file_name }}
              </span>
              <div v-if="cat.list.brand_requirement.length" class="po__brands">
                品牌要求：
                <a-tag v-for="(b, i) in cat.list.brand_requirement" :key="i">
                  {{ b.brand_cn || b.brand_en }}
                </a-tag>
              </div>
            </div>
            <div v-else class="po__muted">
              还没有已确认的采购清单——没有清单就没有招标侧真值，本品类只能做预览比价。
              <a-button type="link" size="small" @click="goCompare">去确认清单 →</a-button>
            </div>
          </div>

          <!-- ── C 供应商与轮次 ─────────────────────────────────────── -->
          <div class="po__section">
            <div class="po__section-title"><TeamOutlined /> 供应商与轮次</div>

            <template v-if="currentRoundOf(cat)?.submissions.length">
              <a-table
                size="small" :pagination="false" row-key="submission_id"
                :data-source="currentRoundOf(cat)!.submissions"
                :columns="[
                  { title: '供应商', dataIndex: 'supplier_name', key: 'name' },
                  { title: '行数', dataIndex: 'line_count', key: 'lines', width: 80 },
                  { title: '明细合计', key: 'detail', width: 150 },
                  { title: '文件声明总价', key: 'declared', width: 170 },
                  { title: '入库时间', key: 'at', width: 150 },
                ]"
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
            </template>
            <div v-else class="po__muted">本轮还没有已入库的报价。</div>

            <!-- 历史轮次：只给报价清单，不给任何结论（D-2）。
                 概述端点本身就不产出排名/推荐字段，所以这不是"选择不显示"，
                 是结构上没有可显示的结论——理由见 design/45 §2.1。 -->
            <div v-if="closedRounds(cat).length" class="po__history">
              <div class="po__history-title">历史轮次</div>
              <div v-for="r in closedRounds(cat)" :key="r.id" class="po__history-row">
                <a-tag>第{{ r.seq }}轮</a-tag>
                <span class="po__muted">{{ r.name }}</span>
                <span class="po__muted">{{ r.submissions.length }} 家报价</span>
                <span class="po__muted po__history-names">
                  {{ r.submissions.map(s => s.supplier_name).join('、') || '—' }}
                </span>
                <span class="po__muted">关闭于 {{ fmtDate(r.closed_at) }}</span>
              </div>
              <div class="po__hint">
                历史轮次只列报价清单。当时的结论不做留存展示——重算会随清单改版、
                供应商合并而漂移，显示一个会变的"当时结论"比不显示更糟。
              </div>
            </div>
          </div>

          <!-- ── D 当前轮比价建议（懒加载，只对锚点轴渲染）───────────── -->
          <CategoryRecommendation
            :project-id="projectId"
            :category="cat.category"
            :axis-kind="cat.axis_kind"
            :submission-count="cat.submission_count"
            @go-align="goAlign(cat.category)"
          />
        </a-card>
      </template>
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
    margin-bottom: 12px;
  }

  &__title { margin: 0; font-size: 20px; font-weight: 600; color: @heading-color; }

  &__sub { margin-top: 4px; font-size: 12px; color: @text-color-secondary; }

  &__muted { color: @text-color-secondary; font-size: 12px; }

  &__strip { margin-bottom: 12px; }

  &__cat { margin-bottom: 16px; }

  &__cat-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 4px;
  }

  &__cat-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

  &__cat-round { font-size: 13px; display: inline-flex; align-items: center; gap: 6px; }

  &__section {
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid @border-color-split;
  }

  &__section-title {
    font-size: 13px; font-weight: 600; margin-bottom: 8px;
    display: flex; align-items: center; gap: 6px;
  }

  &__list-meta { font-size: 13px; }

  &__file { margin-left: 8px; }

  &__brands { margin-top: 6px; font-size: 12px; }

  &__delta { margin-left: 6px; }

  &__history { margin-top: 12px; }

  &__history-title { font-size: 12px; color: @text-color-secondary; margin-bottom: 6px; }

  &__history-row {
    display: flex; align-items: center; gap: 12px;
    padding: 4px 0; font-size: 12px; flex-wrap: wrap;
  }

  &__history-names { flex: 1; min-width: 0; }

  &__hint {
    margin-top: 6px; font-size: 12px; color: @text-color-secondary; opacity: 0.8;
  }
}
</style>
