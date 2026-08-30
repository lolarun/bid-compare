<!--
  design/45 §5.2 D —— 概述页的「当前轮比价建议」。

  三条硬约束在这个组件上体现得最集中，改之前务必读 design/45 §3：

  **C1 — 不得推荐中标人。** `get_evaluation_policy` 对所有项目返回 UNKNOWN
  （评标办法必须来自招标文件，系统不得自造）。所以这里能显示的上限是：
  评标总价排名 + 三态门禁 + 证据缺口 + 风险。**不出"建议选 X"**。八项非价格
  因素没有权重时，显示「综合评审待评标小组确认」，LLM 也不得代为补结论。

  **C3 — 报价派生轴不出结论。** `axis_kind !== 'tender_anchor'` 直接不渲染
  结论区：没有已确认采购清单就没有招标侧真值，能说"这几家同一行报价不一样"，
  不能说"某家漏报了招标要求的项目"。

  **C4 — 同一份业务结果。** 数据来自 `POST /api/analysis/bid-matrix`，就是
  比价矩阵页调的那一个端点。**不得**在这里另写一套便宜的"大致谁便宜"——那正是
  两套口径开始漂移的地方。代价是这一块要单独加载，所以它懒加载、可折叠。
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import { BulbOutlined, WarningOutlined } from '@ant-design/icons-vue'
import { analysisApi } from '@/api'
import type { BidMatrixResult } from '@/api/client'
import { formatMoney } from '@/utils/docCards'

const props = defineProps<{
  projectId: number
  category: string
  axisKind: 'tender_anchor' | 'quote_derived' | null
  submissionCount: number
}>()
defineEmits<{ (e: 'goAlign'): void }>()

const result = ref<BidMatrixResult | null>(null)
const loading = ref(false)
const loaded = ref(false)
const error = ref('')

/** 只有锚点轴才允许出结论（C3）。报价派生轴、以及还没有报价的品类，
 *  连"加载"按钮都不给——不是藏起来，是这里本来就没有可给的正式结论。 */
const canConclude = computed(
  () => props.axisKind === 'tender_anchor' && props.submissionCount > 0,
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: props.projectId, supplier_ids: [], category: props.category,
    })
    result.value = data
    loaded.value = true
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '比价结果加载失败'
    message.error(error.value)
  } finally {
    loading.value = false
  }
}

const LEVEL_META: Record<string, { color: string; type: 'success' | 'warning' | 'error'; text: string }> = {
  firm: { color: 'green', type: 'success', text: '正式推荐' },
  conditional: { color: 'orange', type: 'warning', text: '条件推荐 · 不能完成最终采购确认' },
  blocked: { color: 'red', type: 'error', text: '已阻断 · 仅说明原因，不作推荐' },
}
const levelMeta = computed(() => {
  const lv = result.value?.recommendation_level
  return lv ? LEVEL_META[lv] : null
})

/** 排名只列 `eligible_for_ranking` 的供应商；其余在"未纳入排名"里单独交代，
 *  不是悄悄从表里消失（design/45 §5.2 D：被排除的行要报金额影响）。 */
const ranking = computed(
  () => (result.value?.price_ranking ?? []).filter(s => s.eligible_for_ranking),
)
const notRanked = computed(
  () => (result.value?.price_ranking ?? []).filter(s => !s.eligible_for_ranking),
)

/** 未决行的金额影响合计——「2 处缺口」这种纯计数读不出严重性，
 *  金额才读得出。 */
const undecidedAmount = computed(() =>
  (result.value?.supplier_evaluation ?? []).reduce((a, s) => a + (s.undecided_amount || 0), 0),
)
</script>

<template>
  <div class="cr">
    <div class="cr__title"><BulbOutlined /> 当前轮比价建议</div>

    <!-- C3：报价派生轴 / 无报价 —— 结构上就没有正式结论 -->
    <div v-if="!canConclude" class="cr__muted">
      <template v-if="props.submissionCount === 0">
        还没有已入库的报价，暂无可比结果。
      </template>
      <template v-else>
        本品类没有已确认的采购清单，行轴由某一家报价派生而来，
        <b>只能进预览通道</b>——它能显示各家同一行报价的差异，但不能说明谁漏报了
        招标要求的项目（没有任何东西记录了"应该有什么"）。
      </template>
    </div>

    <template v-else>
      <div v-if="!loaded" class="cr__load">
        <a-button type="primary" ghost :loading="loading" @click="load">
          计算本轮比价结果
        </a-button>
        <span class="cr__muted">
          走的是比价矩阵页那一个端点，结果与矩阵页逐字一致；单独加载是因为它要跑完整对齐。
        </span>
      </div>

      <a-spin :spinning="loading">
        <template v-if="loaded && result">
          <!-- 三态门禁横幅：blocked 也照样展示，只是改成解释阻断原因 -->
          <a-alert
            v-if="levelMeta" :type="levelMeta.type" show-icon class="cr__banner"
            :message="levelMeta.text"
          >
            <template v-if="result.recommendation_reasons?.length" #description>
              <ul class="cr__reasons">
                <li v-for="(r, i) in result.recommendation_reasons" :key="i">{{ r }}</li>
              </ul>
            </template>
          </a-alert>

          <!-- 评标总价排名。标题写死「评标总价排名」，不写「推荐」「优选」：
               招标文件没有给评分公式，系统没有完成任何官方评分（C1）。 -->
          <div class="cr__block">
            <div class="cr__block-title">
              评标总价排名
              <a-tooltip>
                <template #title>
                  按评标总价从低到高排列，<b>不等于</b>官方评标得分——本项目招标文件
                  为合理低价评标价法且未给出权重，系统不得自造评分公式。
                </template>
                <span class="cr__hint">这是什么？</span>
              </a-tooltip>
            </div>
            <a-table
              v-if="ranking.length" size="small" :pagination="false" row-key="submission_id"
              :data-source="ranking"
              :columns="[
                { title: '#', key: 'idx', width: 48 },
                { title: '供应商', dataIndex: 'name', key: 'name' },
                { title: '评标总价', key: 'total', width: 150 },
                { title: '已确认行', key: 'lines', width: 120 },
                { title: '未决金额', key: 'undecided', width: 140 },
              ]"
            >
              <template #bodyCell="{ column, record, index }">
                <template v-if="column.key === 'idx'">{{ index + 1 }}</template>
                <template v-else-if="column.key === 'total'">
                  {{ formatMoney(record.evaluated_total) }}
                </template>
                <template v-else-if="column.key === 'lines'">
                  {{ record.confirmed_lines }} / {{ record.total_anchors }}
                </template>
                <template v-else-if="column.key === 'undecided'">
                  <span v-if="!record.undecided_amount" class="cr__muted">—</span>
                  <span v-else class="cr__warn">
                    {{ formatMoney(record.undecided_amount) }}
                    （{{ record.undecided_lines }} 行）
                  </span>
                </template>
              </template>
            </a-table>
            <div v-else class="cr__muted">没有可纳入排名的供应商。</div>
          </div>

          <!-- 被排除的必须交代清楚，且带金额——计数读不出严重性 -->
          <div v-if="notRanked.length || undecidedAmount" class="cr__block">
            <div class="cr__block-title"><WarningOutlined /> 未纳入与未决</div>
            <div v-if="undecidedAmount" class="cr__excluded">
              全部供应商合计未决金额 <b class="cr__warn">{{ formatMoney(undecidedAmount) }}</b>
              ——这些行未参与评标总价。
              <a-button type="link" size="small" @click="$emit('goAlign')">去复核 →</a-button>
            </div>
            <div v-for="s in notRanked" :key="s.submission_id ?? s.id" class="cr__excluded">
              <b>{{ s.name }}</b> 未纳入排名：
              已确认 {{ s.confirmed_lines }}/{{ s.total_anchors }} 行，
              缺 {{ s.missing_lines }} 行，
              未决 {{ s.undecided_lines }} 行（{{ formatMoney(s.undecided_amount) }}），
              checksum {{ s.checksum_status }}。
            </div>
          </div>

          <!-- C1 的兜底出口：非价格因素无权重时，综合结论只能交回评标小组 -->
          <a-alert
            v-if="result.committee_required !== false"
            type="info" show-icon class="cr__committee"
            message="综合评审待评标小组确认"
            :description="result.comprehensive_recommendation_status
              || '招标文件未给出非价格因素的权重，系统不产出综合推荐候选人，也不得自动定标。'"
          />

          <div v-if="result.risks?.length" class="cr__block">
            <div class="cr__block-title">风险提示</div>
            <ul class="cr__reasons">
              <li v-for="(r, i) in result.risks" :key="i">{{ r }}</li>
            </ul>
          </div>
        </template>
      </a-spin>
    </template>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.cr {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid @border-color-split;

  &__title {
    font-size: 13px; font-weight: 600; margin-bottom: 8px;
    display: flex; align-items: center; gap: 6px;
  }

  &__muted { color: @text-color-secondary; font-size: 12px; }

  // 「需关注」用主题里既有的黄，不另起一个变量名——三级色标
  // （@alert-normal/yellow/red）是全站报价告警的统一语义。
  &__warn { color: @alert-yellow-color; }

  &__load { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }

  &__banner { margin-bottom: 12px; }

  &__block { margin-top: 12px; }

  &__block-title {
    font-size: 12px; font-weight: 600; margin-bottom: 6px;
    display: flex; align-items: center; gap: 6px;
  }

  &__hint {
    font-weight: 400; color: @text-color-secondary;
    text-decoration: underline dotted; cursor: help;
  }

  &__reasons { margin: 0; padding-left: 18px; font-size: 12px; }

  &__excluded { font-size: 12px; padding: 2px 0; }

  &__committee { margin-top: 12px; }
}
</style>
