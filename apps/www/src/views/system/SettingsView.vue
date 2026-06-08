<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { SaveOutlined } from '@ant-design/icons-vue'
import { configApi } from '@/api'

const activeTab = ref<'weights' | 'thresholds'>('weights')

// ─── 评分权重 ────────────────────────────────────────────────────────────
const weights = reactive({
  price_competitiveness: 0.45,
  history_cooperation: 0.25,
  quote_completeness: 0.15,
  commercial_terms: 0.15,
})
const weightLabels: Record<string, string> = {
  price_competitiveness: '价格竞争力',
  history_cooperation: '历史合作',
  quote_completeness: '报价完整度',
  commercial_terms: '商务条款',
}
const weightsLoading = ref(false)

async function saveWeights() {
  const sum = Object.values(weights).reduce((s, v) => s + v, 0)
  if (Math.abs(sum - 1) > 0.001) {
    message.warning(`权重合计 ${(sum * 100).toFixed(0)}%，需等于 100%`)
    return
  }
  weightsLoading.value = true
  try {
    await configApi.update('scoring_weights', { value: weights as Record<string, number> })
    message.success('已保存')
  } catch {
    message.error('保存失败')
  } finally {
    weightsLoading.value = false
  }
}

// ─── 偏差阈值（分品类）───────────────────────────────────────────────────
interface ThresholdRow {
  category: string
  yellow: number
  red: number
}

const thresholds = ref<ThresholdRow[]>([
  { category: 'default', yellow: 0.05, red: 0.10 },
  { category: '桥架', yellow: 0.08, red: 0.15 },
  { category: '阀门', yellow: 0.06, red: 0.12 },
  { category: '配电箱', yellow: 0.05, red: 0.10 },
  { category: '不锈钢管', yellow: 0.05, red: 0.10 },
  { category: '水箱', yellow: 0.08, red: 0.15 },
  { category: '潜水泵', yellow: 0.06, red: 0.12 },
  { category: '风口风阀', yellow: 0.07, red: 0.13 },
  { category: '风机盘管', yellow: 0.07, red: 0.13 },
  { category: '空调泵', yellow: 0.06, red: 0.12 },
  { category: '母线槽', yellow: 0.06, red: 0.12 },
])

const thresholdsLoading = ref(false)
async function saveThresholds() {
  thresholdsLoading.value = true
  try {
    const value: Record<string, { yellow: number; red: number }> = {}
    for (const t of thresholds.value) value[t.category] = { yellow: t.yellow, red: t.red }
    await configApi.update('thresholds', { value })
    message.success('阈值已保存')
  } catch {
    message.error('保存失败')
  } finally {
    thresholdsLoading.value = false
  }
}

const thresholdColumns = [
  { title: '品类', dataIndex: 'category', width: 130 },
  { title: '黄色预警阈值（需关注）', dataIndex: 'yellow' },
  { title: '红色预警阈值（异常）', dataIndex: 'red' },
]

onMounted(async () => {
  try {
    const { data } = await configApi.list()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = (data as any[]).find?.((x) => x.key === 'scoring_weights')
    if (w?.value) {
      // 仅采用当前四维权重，忽略历史遗留的 brand_compliance
      for (const k of Object.keys(weights) as Array<keyof typeof weights>) {
        if (typeof w.value[k] === 'number') weights[k] = w.value[k]
      }
    }
  } catch {
    // 后端可能未就绪
  }
})
</script>

<template>
  <div class="settings-page">
    <div class="settings-page__header">
      <div>
        <h1 class="settings-page__title">系统设置</h1>
        <div class="settings-page__subtitle">评分权重、偏差阈值 · 影响比价算法的关键参数</div>
      </div>
    </div>

    <a-card :body-style="{ padding: '0 0 16px 0' }">
      <a-tabs v-model:active-key="activeTab" :tab-bar-style="{ padding: '0 20px', marginBottom: 0 }">
        <!-- 评分权重 -->
        <a-tab-pane key="weights" tab="评分权重">
          <div class="tab-body">
            <a-alert
              type="info"
              show-icon
              message="四维评分权重合计必须为 100%。调整后即时生效，影响新一轮供应商画像计算。"
              style="margin-bottom:16px"
            />
            <div class="weight-form">
              <div v-for="(_, k) in weights" :key="k" class="weight-row">
                <span class="weight-row__label">{{ weightLabels[k] }}</span>
                <a-slider
                  v-model:value="weights[k]"
                  :min="0"
                  :max="1"
                  :step="0.01"
                  :tip-formatter="(v: number | undefined) => v ? `${(v * 100).toFixed(0)}%` : ''"
                  class="weight-row__slider"
                />
                <a-input-number
                  v-model:value="weights[k]"
                  :min="0"
                  :max="1"
                  :step="0.05"
                  :formatter="(v: number | string | undefined) => v ? `${(Number(v) * 100).toFixed(0)}%` : ''"
                  :parser="(v: string | undefined) => v ? Number(v.replace('%','')) / 100 : 0"
                  style="width:90px"
                />
              </div>
              <div class="weight-row__footer">
                <span>合计：<strong>{{ (Object.values(weights).reduce((s, v) => s + v, 0) * 100).toFixed(0) }}%</strong></span>
                <a-button type="primary" :loading="weightsLoading" @click="saveWeights">
                  <template #icon><SaveOutlined /></template>
                  保存权重
                </a-button>
              </div>
            </div>
          </div>
        </a-tab-pane>

        <!-- 偏差阈值 -->
        <a-tab-pane key="thresholds" tab="偏差阈值（分品类）">
          <div class="tab-body">
            <a-alert
              type="info"
              show-icon
              message="偏差率 ≤ 黄色阈值显示无色，黄色 ~ 红色之间显示黄色，超过红色阈值显示红色。default 行作为兜底配置。"
              style="margin-bottom:16px"
            />
            <a-table
              :columns="thresholdColumns"
              :data-source="thresholds"
              :pagination="false"
              row-key="category"
              size="middle"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.dataIndex === 'yellow'">
                  <a-input-number
                    v-model:value="(record as ThresholdRow).yellow"
                    :min="0"
                    :max="1"
                    :step="0.01"
                    :formatter="(v: number | string | undefined) => v ? `${(Number(v) * 100).toFixed(0)}%` : ''"
                    :parser="(v: string | undefined) => v ? Number(v.replace('%','')) / 100 : 0"
                    style="width:120px"
                  />
                </template>
                <template v-else-if="column.dataIndex === 'red'">
                  <a-input-number
                    v-model:value="(record as ThresholdRow).red"
                    :min="0"
                    :max="1"
                    :step="0.01"
                    :formatter="(v: number | string | undefined) => v ? `${(Number(v) * 100).toFixed(0)}%` : ''"
                    :parser="(v: string | undefined) => v ? Number(v.replace('%','')) / 100 : 0"
                    style="width:120px"
                  />
                </template>
              </template>
            </a-table>
            <div style="margin-top:16px;text-align:right">
              <a-button type="primary" :loading="thresholdsLoading" @click="saveThresholds">
                <template #icon><SaveOutlined /></template>
                保存阈值
              </a-button>
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.settings-page {
  &__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 16px;
  }
  &__title {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: @heading-color;
  }
  &__subtitle {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }
}

.tab-body {
  padding: 16px 20px;
}

.weight-form {
  max-width: 600px;
}
.weight-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 0;

  &__label {
    width: 120px;
    font-size: 13px;
    color: @text-color;
  }

  &__slider {
    flex: 1;
  }

  &__footer {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid @border-color-split;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
}
</style>
