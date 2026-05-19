<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import {
  EditOutlined,
  PlusOutlined,
  DeleteOutlined,
  SaveOutlined,
} from '@ant-design/icons-vue'
import { configApi } from '@/api'

const activeTab = ref<'weights' | 'thresholds' | 'brand_tiers'>('weights')

// ─── 评分权重 ────────────────────────────────────────────────────────────
const weights = reactive({
  price: 0.40,
  history: 0.20,
  completeness: 0.15,
  brand: 0.15,
  commercial: 0.10,
})
const weightLabels: Record<string, string> = {
  price: '价格竞争力',
  history: '历史合作',
  completeness: '报价完整度',
  brand: '品牌合规',
  commercial: '商务条款',
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

// ─── 品牌档位映射 ────────────────────────────────────────────────────────
interface BrandTier {
  id: number
  brand_name: string
  tier: '一档' | '二档' | '三档'
  category: string | null
}

const brandTiers = ref<BrandTier[]>([
  { id: 1, brand_name: '鞍钢', tier: '一档', category: '钢管' },
  { id: 2, brand_name: '宝钢', tier: '一档', category: '钢管' },
  { id: 3, brand_name: '伟星新材', tier: '一档', category: 'PPR 管' },
  { id: 4, brand_name: '联塑', tier: '一档', category: 'PPR 管' },
  { id: 5, brand_name: '良工', tier: '一档', category: '阀门' },
  { id: 6, brand_name: '苏阀', tier: '二档', category: '阀门' },
  { id: 7, brand_name: '正泰电器', tier: '一档', category: '配电箱' },
  { id: 8, brand_name: '德力西', tier: '一档', category: '配电箱' },
  { id: 9, brand_name: '上海二工', tier: '二档', category: '配电箱' },
  { id: 10, brand_name: '海德隆', tier: '二档', category: '水箱' },
  { id: 11, brand_name: '江苏华润', tier: '一档', category: '桥架' },
])

const brandModalVisible = ref(false)
const brandForm = reactive({
  id: null as number | null,
  brand_name: '',
  tier: '一档' as BrandTier['tier'],
  category: '',
})

function openBrandCreate() {
  Object.assign(brandForm, { id: null, brand_name: '', tier: '一档', category: '' })
  brandModalVisible.value = true
}

function openBrandEdit(b: BrandTier) {
  Object.assign(brandForm, {
    id: b.id,
    brand_name: b.brand_name,
    tier: b.tier,
    category: b.category || '',
  })
  brandModalVisible.value = true
}

function saveBrand() {
  if (!brandForm.brand_name) {
    message.warning('请填写品牌名')
    return
  }
  if (brandForm.id) {
    const t = brandTiers.value.find((b) => b.id === brandForm.id)
    if (t) {
      t.brand_name = brandForm.brand_name
      t.tier = brandForm.tier
      t.category = brandForm.category || null
    }
  } else {
    brandTiers.value.push({
      id: Math.max(...brandTiers.value.map((b) => b.id), 0) + 1,
      brand_name: brandForm.brand_name,
      tier: brandForm.tier,
      category: brandForm.category || null,
    })
  }
  brandModalVisible.value = false
  message.success('已保存')
}

function removeBrand(id: number) {
  brandTiers.value = brandTiers.value.filter((b) => b.id !== id)
  message.success('已删除')
}

const brandColumns = [
  { title: '品牌名', dataIndex: 'brand_name', width: 160 },
  { title: '档位', dataIndex: 'tier', width: 100 },
  { title: '适用品类', dataIndex: 'category', width: 140,
    customRender: ({ text }: { text: string | null }) => text || '— 通用 —' },
  { title: '操作', key: 'action', width: 140, fixed: 'right' as const },
]

function tierColor(t: BrandTier['tier']) {
  return t === '一档' ? 'gold' : t === '二档' ? 'blue' : 'default'
}

onMounted(async () => {
  try {
    const { data } = await configApi.list()
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const w = (data as any[]).find?.((x) => x.key === 'scoring_weights')
    if (w?.value) Object.assign(weights, w.value)
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
        <div class="settings-page__subtitle">权重、偏差阈值、品牌档位映射 · 影响比价算法的关键参数</div>
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
              message="五维评分权重合计必须为 100%。调整后即时生效，影响新一轮供应商画像计算。"
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

        <!-- 品牌档位 -->
        <a-tab-pane key="brand_tiers" tab="品牌档位映射">
          <div class="tab-body">
            <div class="flex flex-between" style="margin-bottom:12px">
              <a-alert
                type="info"
                show-icon
                message="品牌作为比价匹配项，按一档/二档/三档区分；新品牌可在「采购价格导入」首次出现时弹窗写入。"
                style="flex:1;margin-right:12px"
              />
              <a-button type="primary" @click="openBrandCreate">
                <template #icon><PlusOutlined /></template>
                新增品牌
              </a-button>
            </div>
            <a-table
              :columns="brandColumns"
              :data-source="brandTiers"
              :pagination="{ pageSize: 10, showTotal: (t: number) => `共 ${t} 个品牌` }"
              row-key="id"
              size="middle"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.dataIndex === 'tier'">
                  <a-tag :color="tierColor((record as BrandTier).tier)">{{ (record as BrandTier).tier }}</a-tag>
                </template>
                <template v-else-if="column.key === 'action'">
                  <a-space>
                    <a @click="openBrandEdit(record as BrandTier)"><EditOutlined /> 编辑</a>
                    <a-popconfirm title="确认删除？" @confirm="removeBrand((record as BrandTier).id)">
                      <a style="color:#ff4d4f"><DeleteOutlined /> 删除</a>
                    </a-popconfirm>
                  </a-space>
                </template>
              </template>
            </a-table>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- 品牌弹窗 -->
    <a-modal
      v-model:open="brandModalVisible"
      :title="brandForm.id ? '编辑品牌' : '新增品牌'"
      @ok="saveBrand"
      :width="480"
    >
      <a-form layout="vertical">
        <a-form-item label="品牌名" required>
          <a-input v-model:value="brandForm.brand_name" />
        </a-form-item>
        <a-form-item label="档位">
          <a-radio-group v-model:value="brandForm.tier">
            <a-radio-button value="一档">一档</a-radio-button>
            <a-radio-button value="二档">二档</a-radio-button>
            <a-radio-button value="三档">三档</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="适用品类（可留空 = 通用）">
          <a-input v-model:value="brandForm.category" placeholder="例：桥架 / 阀门" />
        </a-form-item>
      </a-form>
    </a-modal>
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
