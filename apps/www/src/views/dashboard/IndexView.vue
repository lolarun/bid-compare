<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  DatabaseOutlined,
  BarChartOutlined,
  AlertOutlined,
  AppstoreOutlined,
  PlusOutlined,
  ExportOutlined,
  CloudUploadOutlined,
  SearchOutlined,
  EditOutlined,
} from '@ant-design/icons-vue'
import StatCard from '@/components/StatCard.vue'
import PriceTrend from './components/PriceTrend.vue'
import TreeHeatmap from './components/TreeHeatmap.vue'
import BubbleChart from './components/BubbleChart.vue'
import { analysisApi, quoteApi, exportApi } from '@/api'
import type { DashboardSummary, Quote } from '@/api/client'
import { normalizeAlert, alertColors, alertLabels, formatDeviation } from '@/utils/alert'
import { doExport } from '@/utils/download'
import dayjs from 'dayjs'

const router = useRouter()

const summary = ref<DashboardSummary | null>(null)
const recentQuotes = ref<(Quote & { material_name?: string; spec?: string })[]>([])
const loading = ref(false)

async function fetchAll() {
  loading.value = true
  try {
    const [s, q] = await Promise.all([
      analysisApi.dashboard().catch(() => null),
      quoteApi.list({ page: 1, page_size: 7 }).catch(() => null),
    ])
    summary.value = s?.data ?? null
    recentQuotes.value = (q?.data?.items as typeof recentQuotes.value) ?? []
  } finally {
    loading.value = false
  }
}

onMounted(fetchAll)

// ─── Stat cards 数据 ────────────────────────────────────────────────────
const MOCK_STATS = { total: 12847, quotes: 156, warn: 23, profCount: 8, catCount: 342 }

const stats = computed(() => {
  const s = summary.value
  if (!s) return { ...MOCK_STATS, isMock: true }
  return {
    total: s.total_materials,
    quotes: s.total_quotes,
    warn: (s.category_stats ?? []).filter((c) => (c.price_cv ?? 0) > 1).length,
    profCount: new Set((s.category_stats ?? []).map((c) => c.profession)).size,
    catCount: (s.category_stats ?? []).length,
    isMock: false,
  }
})

// ─── 最近比价活动 ────────────────────────────────────────────────────────
interface RecentRow {
  time: string
  name: string
  spec: string
  price: number
  deviation: number
  alert: 'normal' | 'yellow' | 'red'
}
const recentRows = computed<RecentRow[]>(() => {
  if (recentQuotes.value.length === 0) {
    return [
      { time: '04-25 14:23', name: 'DN100 无缝钢管', spec: 'Q235', price: 85, deviation: 0.181, alert: 'red' },
      { time: '04-25 11:08', name: 'PPR 给水管 De32', spec: '', price: 12.5, deviation: -0.023, alert: 'normal' },
      { time: '04-24 16:45', name: '配电箱 XL-21', spec: '', price: 1280, deviation: 0.084, alert: 'yellow' },
      { time: '04-24 09:12', name: '真空断路器 ZW32-12', spec: '室高', price: 8500, deviation: 0.012, alert: 'normal' },
      { time: '04-23 15:30', name: '暖通铜管 φ22×1.0', spec: '紫铜', price: 68, deviation: 0.067, alert: 'yellow' },
    ]
  }
  return recentQuotes.value.map((q) => ({
    time: q.quote_date || dayjs(q.created_at || undefined).format('MM-DD HH:mm'),
    name: q.material_name || `物料 #${q.material_id}`,
    spec: q.spec || '',
    price: q.unit_price ?? 0,
    deviation: q.deviation_pct ?? 0,
    alert: normalizeAlert(q.alert_level),
  }))
})

const recentColumns = [
  { title: '时间', dataIndex: 'time', width: 110 },
  { title: '材料', dataIndex: 'name' },
  { title: '报价', dataIndex: 'price', width: 100, align: 'right' as const },
  { title: '偏差%', dataIndex: 'deviation', width: 90, align: 'right' as const },
  { title: '状态', dataIndex: 'alert', width: 80 },
]

// ─── 快捷入口 ───────────────────────────────────────────────────────────
const quickActions = [
  { icon: PlusOutlined, title: '新建比价分析', desc: '录入本轮报价并对比历史数据', to: '/compare' },
  { icon: CloudUploadOutlined, title: '上传扫描图件', desc: 'PDF/JPG 自动 OCR 入库', to: '/import' },
  { icon: SearchOutlined, title: '查询历史数据', desc: '材料/供应商/项目历史查询', to: '/analysis' },
  { icon: EditOutlined, title: '维护标准库', desc: '完善物料分类与品牌', to: '/materials' },
]

const monthPickerValue = ref(dayjs())
const visTab = ref<'tree' | 'bubble'>('tree')
</script>

<template>
  <div class="dashboard">
    <!-- 标题区 -->
    <div class="dashboard__header">
      <div>
        <h1 class="dashboard__title">
          仪表盘
          <a-tag v-if="stats.isMock" color="orange" style="font-size:11px;margin-left:8px;vertical-align:middle">示例数据</a-tag>
        </h1>
        <div class="dashboard__subtitle">
          {{ monthPickerValue.format('YYYY 年 M 月') }} · 全公司机电材料采购概览
        </div>
      </div>
      <div class="dashboard__actions">
        <a-month-picker v-model:value="monthPickerValue" placeholder="本月" />
        <a-button @click="doExport(() => exportApi.dashboard(), 'MEMPAS_仪表盘报表.xlsx')">
          <template #icon><ExportOutlined /></template>
          导出报表
        </a-button>
      </div>
    </div>

    <!-- 摘要四卡片 -->
    <a-row :gutter="16" class="mb-16">
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="DatabaseOutlined"
          icon-bg="rgba(22,119,255,0.1)"
          label="累计入库材料"
          :value="stats.total.toLocaleString()"
          unit="条"
          :trend="{ value: '+8.2%', positive: true }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="BarChartOutlined"
          icon-bg="rgba(82,196,26,0.1)"
          label="本月比价次数"
          :value="stats.quotes"
          unit="次"
          :trend="{ value: '+23%', positive: true }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="AlertOutlined"
          icon-bg="rgba(255,77,79,0.1)"
          label="偏差预警"
          :value="stats.warn"
          unit="项"
          :trend="{ value: '需关注', danger: true, label: '高偏差' }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="AppstoreOutlined"
          icon-bg="rgba(250,173,20,0.1)"
          label="数据库覆盖品类"
          :value="stats.profCount"
          unit="个专业 / 342 类"
          :trend="{ value: '覆盖完整', positive: true, label: '✓' }"
        />
      </a-col>
    </a-row>

    <!-- 最近比价活动 + 价格趋势 -->
    <a-row :gutter="16" class="mb-16">
      <a-col :xs="24" :lg="14">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <span style="font-size:15px;font-weight:600">最近比价活动</span>
            <span style="font-size:12px;color:rgba(0,0,0,0.45);margin-left:8px">展示近 7 天内提交的比价分析</span>
          </template>
          <template #extra>
            <a @click="router.push('/analysis')">查看全部 →</a>
          </template>
          <a-table
            :columns="recentColumns"
            :data-source="recentRows"
            :pagination="false"
            size="middle"
            :row-key="(r: RecentRow) => r.time + r.name"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.dataIndex === 'name'">
                <span style="font-weight:500">{{ record.name }}</span>
                <span v-if="record.spec" style="color:rgba(0,0,0,0.45);margin-left:6px">/ {{ record.spec }}</span>
              </template>
              <template v-else-if="column.dataIndex === 'price'">
                ¥{{ record.price.toLocaleString() }}
              </template>
              <template v-else-if="column.dataIndex === 'deviation'">
                <span :style="{ color: alertColors[(record as RecentRow).alert] }">
                  {{ formatDeviation((record as RecentRow).deviation) }}
                </span>
              </template>
              <template v-else-if="column.dataIndex === 'alert'">
                <a-tag :color="(record as RecentRow).alert === 'red' ? 'red' : (record as RecentRow).alert === 'yellow' ? 'orange' : 'green'">
                  {{ alertLabels[(record as RecentRow).alert] }}
                </a-tag>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="10">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <span style="font-size:15px;font-weight:600">高频材料价格趋势</span>
            <span style="font-size:12px;color:rgba(0,0,0,0.45);margin-left:8px">近 6 个月均价走势</span>
          </template>
          <template #extra>
            <a-select :value="'6m'" size="small" style="width:100px">
              <a-select-option value="6m">近 6 个月</a-select-option>
              <a-select-option value="12m">近 12 个月</a-select-option>
            </a-select>
          </template>
          <PriceTrend :loading="loading" />
        </a-card>
      </a-col>
    </a-row>

    <!-- 项目/品类可视化（F1.2 / F1.3）-->
    <a-card :body-style="{ padding: '16px 20px' }" class="mb-16">
      <template #title>
        <span style="font-size:15px;font-weight:600">项目 / 品类全景</span>
        <span style="font-size:12px;color:rgba(0,0,0,0.45);margin-left:8px">
          {{ visTab === 'tree' ? '树状热力图：项目 → 品类 → 金额' : '气泡图：品类 → 专业 → 金额' }}
        </span>
      </template>
      <template #extra>
        <a-radio-group v-model:value="visTab" size="small">
          <a-radio-button value="tree">树状热力图</a-radio-button>
          <a-radio-button value="bubble">气泡图</a-radio-button>
        </a-radio-group>
      </template>
      <TreeHeatmap v-if="visTab === 'tree'" :loading="loading" />
      <BubbleChart v-else :loading="loading" />
    </a-card>

    <!-- 快捷入口 -->
    <a-row :gutter="16">
      <a-col v-for="qa in quickActions" :key="qa.title" :xs="24" :sm="12" :md="6">
        <a-card
          hoverable
          class="quick-action"
          :body-style="{ padding: '16px 20px' }"
          @click="router.push(qa.to)"
        >
          <div class="quick-action__icon">
            <component :is="qa.icon" />
          </div>
          <div>
            <div class="quick-action__title">{{ qa.title }}</div>
            <div class="quick-action__desc">{{ qa.desc }}</div>
          </div>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.dashboard {
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

  &__actions {
    display: flex;
    gap: 8px;
  }
}

.quick-action {
  border-radius: @border-radius-lg;
  display: flex;
  align-items: center;
  gap: 14px;

  :deep(.ant-card-body) {
    display: flex;
    align-items: center;
    gap: 14px;
    width: 100%;
  }

  &__icon {
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: rgba(22, 119, 255, 0.1);
    color: @primary-color;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    flex-shrink: 0;
  }

  &__title {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
    margin-bottom: 2px;
  }

  &__desc {
    font-size: 12px;
    color: @text-color-secondary;
  }
}
</style>
