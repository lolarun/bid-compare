<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import {
  HistoryOutlined,
  SaveOutlined,
  ExportOutlined,
  ThunderboltOutlined,
  CheckCircleOutlined,
  PlusOutlined,
  DeleteOutlined,
  RobotOutlined,
} from '@ant-design/icons-vue'

// ─── 招标信息表单 ────────────────────────────────────────────────────────
interface SpecRow {
  key: string
  name: string
  qty: number | undefined
  unit: string
}

const form = reactive({
  project_name: '项目 Y · 数据中心电缆桥架采购',
  category: '桥架类 / 托盘式桥架',
  bid_qty: 1300,
  unit: '米',
  budget_max: 160000,
})

const specs = ref<SpecRow[]>([
  { key: 'a', name: '托盘式桥架直通 300×150', qty: 800, unit: '米' },
  { key: 'b', name: '托盘式桥架直通 600×200', qty: 320, unit: '米' },
  { key: 'c', name: '托盘式防火桥架 400×150', qty: 180, unit: '米' },
])

function addSpec() {
  specs.value.push({ key: `r${Date.now()}`, name: '', qty: undefined, unit: '米' })
}

function removeSpec(k: string) {
  specs.value = specs.value.filter((s) => s.key !== k)
}

// ─── AI 推荐结果（mock）───────────────────────────────────────────────────
interface RecommendedSupplier {
  id: number
  letter: string
  name: string
  ai_score: number
  ai_reason: string
  price_advantage: number  // 0.052
  win_count: number
  delivery_score: number
  tags: { label: string; color: string }[]
  added: boolean
}

const recommended = ref<RecommendedSupplier[]>([
  {
    id: 1, letter: '江', name: '江苏华润管业', ai_score: 92,
    ai_reason: '该公司供应商类匹配优秀且良品价格优势，近 3 年 8 次合作均按时交付，建议作为本次招标主选供应商之一。',
    price_advantage: -0.052, win_count: 8, delivery_score: 0.95,
    tags: [{ label: '价格优势', color: 'green' }, { label: '稳定合作', color: 'blue' }],
    added: true,
  },
  {
    id: 2, letter: '上', name: '上海管业贸易', ai_score: 88,
    ai_reason: '近一年报价稳健，报价低稳价 7.5%，价格优势明显，但 3 次少量交付，质量稳定一次进过，建议作为价格补充供应。',
    price_advantage: -0.075, win_count: 3, delivery_score: 0.90,
    tags: [{ label: '价格优势', color: 'green' }, { label: '新合作', color: 'purple' }],
    added: true,
  },
  {
    id: 3, letter: '天', name: '天源华威桥架', ai_score: 86,
    ai_reason: '专业桥架制造商，报价稳定且质量过硬，火桥架资源丰富，履约率全场最高（98%），适合关键节点项目。',
    price_advantage: 0.018, win_count: 6, delivery_score: 0.98,
    tags: [{ label: '质量优秀', color: 'cyan' }, { label: '稳定供应', color: 'blue' }],
    added: true,
  },
  {
    id: 4, letter: '广', name: '广东联墅供应', ai_score: 81,
    ai_reason: '管材类长期合作（4 次）历史比价中等，可作为补充报价候选，可补充作为采购流入价的桥架以加大议价空间。',
    price_advantage: -0.036, win_count: 4, delivery_score: 0.88,
    tags: [{ label: '补充候选', color: 'default' }],
    added: false,
  },
  {
    id: 5, letter: '江', name: '江苏华润电气', ai_score: 78,
    ai_reason: '防火桥架资质完整，履约成功偏低（85%），建议作为防火桥架专项采购备选清单，主选不推荐。',
    price_advantage: 0.024, win_count: 5, delivery_score: 0.85,
    tags: [{ label: '防火专项', color: 'orange' }],
    added: false,
  },
])

const generating = ref(false)
async function generate() {
  generating.value = true
  // 实际接入：POST /api/invite/recommend
  setTimeout(() => { generating.value = false }, 800)
}

const selectedCount = computed(() => recommended.value.filter((s) => s.added).length)
const totalCount = computed(() => recommended.value.length)

function toggleAdd(s: RecommendedSupplier) {
  s.added = !s.added
}

function scoreColor(score: number) {
  if (score >= 85) return '#52c41a'
  if (score >= 75) return '#1677ff'
  if (score >= 60) return '#faad14'
  return '#ff4d4f'
}

function fmtPct(d: number) {
  const pct = (d * 100).toFixed(1)
  return d >= 0 ? `+${pct}%` : `${pct}%`
}
</script>

<template>
  <div class="invite-page">
    <!-- 标题区 -->
    <div class="invite-page__header">
      <div>
        <h1 class="invite-page__title">邀标建议</h1>
        <div class="invite-page__subtitle">
          填写招标信息生成邀标 · 历史采购、供应商画像、AI 推荐多维加权建议
        </div>
      </div>
      <a-button>
        <template #icon><HistoryOutlined /></template>
        历史邀标
      </a-button>
    </div>

    <a-row :gutter="16">
      <!-- 左栏：招标信息 -->
      <a-col :xs="24" :lg="8">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <span style="font-size:15px;font-weight:600">招标信息</span>
          </template>
          <a-form layout="vertical">
            <a-form-item label="项目名称">
              <a-input v-model:value="form.project_name" />
            </a-form-item>
            <a-form-item label="物料类别">
              <a-cascader
                :default-value="['桥架类','托盘式桥架']"
                :options="[
                  { value: '桥架类', label: '桥架类', children: [
                    { value: '托盘式桥架', label: '托盘式桥架' },
                    { value: '梯式桥架', label: '梯式桥架' },
                    { value: '托盘式防火桥架', label: '托盘式防火桥架' },
                  ]},
                  { value: '管材类', label: '管材类' },
                  { value: '电气类', label: '电气类' },
                ]"
              />
            </a-form-item>

            <div style="margin-bottom:6px;font-size:13px;color:rgba(0,0,0,0.65)">规格行</div>
            <div class="spec-list">
              <div v-for="s in specs" :key="s.key" class="spec-row">
                <a-input v-model:value="s.name" placeholder="规格描述" />
                <a-input-number v-model:value="s.qty" :min="0" placeholder="数量" style="width:90px" />
                <a-input v-model:value="s.unit" style="width:60px" />
                <a-button type="text" danger size="small" @click="removeSpec(s.key)">
                  <DeleteOutlined />
                </a-button>
              </div>
              <a-button type="dashed" block @click="addSpec">
                <PlusOutlined />
                添加规格
              </a-button>
            </div>

            <a-row :gutter="12" style="margin-top:14px">
              <a-col :span="12">
                <a-form-item label="招标总量">
                  <a-input-number
                    v-model:value="form.bid_qty"
                    :min="0"
                    style="width:100%"
                    :addon-after="form.unit"
                  />
                </a-form-item>
              </a-col>
              <a-col :span="12">
                <a-form-item label="预算上限">
                  <a-input-number
                    v-model:value="form.budget_max"
                    :min="0"
                    style="width:100%"
                    :formatter="(v: number | string | undefined) => v ? `¥${v}` : ''"
                  />
                </a-form-item>
              </a-col>
            </a-row>

            <a-button
              type="primary"
              block
              :loading="generating"
              size="large"
              @click="generate"
            >
              <template #icon><ThunderboltOutlined /></template>
              生成邀标建议
            </a-button>
          </a-form>
        </a-card>
      </a-col>

      <!-- 右栏：供应商卡片列表 -->
      <a-col :xs="24" :lg="16">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <div style="display:flex;align-items:center;gap:8px">
              <RobotOutlined style="color:#1677ff" />
              <span style="font-size:15px;font-weight:600">已从供应商库中分析 28 家相似品类</span>
              <span style="font-size:12px;color:rgba(0,0,0,0.45)">
                · 加 <strong style="color:#52c41a">价格优势 60%</strong> +
                <strong style="color:#1677ff">履约评分 40%</strong> 综合评估
              </span>
            </div>
          </template>
          <template #extra>
            <a-button type="link">
              <template #icon><HistoryOutlined /></template>
              历史邀标
            </a-button>
          </template>

          <div class="supplier-card-list">
            <div
              v-for="s in recommended"
              :key="s.id"
              class="supplier-card"
              :class="{ 'supplier-card--added': s.added }"
            >
              <div class="supplier-card__letter" :style="{ background: scoreColor(s.ai_score) }">
                {{ s.letter }}
              </div>
              <div class="supplier-card__body">
                <div class="supplier-card__top">
                  <div>
                    <div class="supplier-card__name">{{ s.name }}</div>
                    <div class="supplier-card__score">
                      <span class="value" :style="{ color: scoreColor(s.ai_score) }">{{ s.ai_score }}</span>
                      <span class="label">AI 综合评分</span>
                    </div>
                  </div>
                  <div class="supplier-card__actions">
                    <a-tag v-if="s.added" color="green">
                      <CheckCircleOutlined />
                      已加入名单
                    </a-tag>
                    <a-button v-else type="primary" size="small" @click="toggleAdd(s)">
                      <PlusOutlined />
                      加入名单
                    </a-button>
                    <a-button v-if="s.added" size="small" @click="toggleAdd(s)">
                      <DeleteOutlined />
                      移除
                    </a-button>
                    <a-button type="link" size="small">查看画像</a-button>
                  </div>
                </div>
                <div class="supplier-card__reason">
                  <RobotOutlined /> AI 推荐理由（DeepSeek）：{{ s.ai_reason }}
                </div>
                <div class="supplier-card__metrics">
                  <span class="metric" :style="{ color: s.price_advantage <= 0 ? '#52c41a' : '#faad14' }">
                    {{ fmtPct(s.price_advantage) }}
                  </span>
                  <span class="metric-label">价格优势</span>
                  <span class="metric">{{ s.win_count }} 次</span>
                  <span class="metric-label">合作次数</span>
                  <span class="metric">{{ Math.round(s.delivery_score * 100) }}%</span>
                  <span class="metric-label">履约评分</span>
                  <a-tag
                    v-for="t in s.tags"
                    :key="t.label"
                    :color="t.color"
                  >{{ t.label }}</a-tag>
                </div>
              </div>
            </div>
          </div>

          <!-- 底部操作栏 -->
          <div class="invite-page__footer">
            <span class="invite-page__count">
              已选供应商：<strong>{{ selectedCount }}</strong> / {{ totalCount }}
            </span>
            <a-space>
              <a-button>
                <template #icon><SaveOutlined /></template>
                保存为草稿
              </a-button>
              <a-button>
                <template #icon><ExportOutlined /></template>
                生成邀标清单 PDF
              </a-button>
              <a-button type="primary">
                <template #icon><ThunderboltOutlined /></template>
                一键发起询价
              </a-button>
            </a-space>
          </div>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.invite-page {
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

  &__footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid @border-color-split;
  }

  &__count {
    font-size: 13px;
    color: @text-color;
  }
}

.spec-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.spec-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.supplier-card-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.supplier-card {
  display: flex;
  gap: 14px;
  padding: 14px 16px;
  border: 1px solid @border-color-base;
  border-radius: @border-radius-lg;
  background: #fff;
  transition: all 0.2s;

  &--added {
    background: rgba(82, 196, 26, 0.04);
    border-color: rgba(82, 196, 26, 0.4);
  }

  &__letter {
    width: 40px;
    height: 40px;
    border-radius: 8px;
    color: #fff;
    font-size: 18px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  &__body {
    flex: 1;
    min-width: 0;
  }

  &__top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }

  &__name {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
  }

  &__score {
    margin-top: 2px;
    display: flex;
    align-items: baseline;
    gap: 6px;

    .value {
      font-size: 20px;
      font-weight: 700;
    }
    .label {
      font-size: 11px;
      color: @text-color-tertiary;
    }
  }

  &__actions {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-shrink: 0;
  }

  &__reason {
    margin-top: 8px;
    font-size: 12px;
    color: @text-color-secondary;
    line-height: 1.6;
  }

  &__metrics {
    margin-top: 8px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 12px;
    font-size: 12px;

    .metric {
      font-weight: 600;
      color: @heading-color;
    }
    .metric-label {
      color: @text-color-tertiary;
      margin-right: 4px;
    }
  }
}
</style>
