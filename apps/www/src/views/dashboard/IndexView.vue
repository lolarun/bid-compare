<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
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
import TreeHeatmap from './components/TreeHeatmap.vue'
import BubbleChart from './components/BubbleChart.vue'
import { analysisApi, exportApi } from '@/api'
import type { DashboardSummary, TreeNode, DashboardBubbleItem } from '@/api/client'
import { doExport } from '@/utils/download'
import dayjs from 'dayjs'

const router = useRouter()

const summary = ref<DashboardSummary | null>(null)
const heatmapNodes = ref<TreeNode[]>([])
const bubbleItems = ref<DashboardBubbleItem[]>([])
const loading = ref(false)

function dateParams() {
  const m = monthPickerValue.value
  if (!m) return {}
  return {
    date_from: m.startOf('month').format('YYYY-MM-DD'),
    date_to: m.endOf('month').format('YYYY-MM-DD'),
  }
}

async function fetchAll() {
  loading.value = true
  try {
    const [sumRes, hmRes, bbRes] = await Promise.all([
      analysisApi.dashboard().catch(() => null),
      analysisApi.heatmap(dateParams()).catch(() => null),
      analysisApi.bubble(dateParams()).catch(() => null),
    ])
    summary.value = sumRes?.data ?? null
    heatmapNodes.value = hmRes?.data?.nodes ?? []
    bubbleItems.value = bbRes?.data?.items ?? []
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

// ─── 快捷入口 ───────────────────────────────────────────────────────────
const quickActions = [
  { icon: PlusOutlined, title: '新建比价分析', desc: '录入本轮报价并对比历史数据', to: '/compare' },
  { icon: CloudUploadOutlined, title: '上传扫描图件', desc: 'PDF/JPG 自动 OCR 入库', to: '/import' },
  { icon: SearchOutlined, title: '历史价格查询', desc: '材料/供应商/项目历史查询', to: '/analysis' },
  { icon: EditOutlined, title: '维护标准库', desc: '完善物料分类与品牌', to: '/materials' },
]

const monthPickerValue = ref<ReturnType<typeof dayjs> | null>(null)
watch(monthPickerValue, fetchAll)
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
          {{ monthPickerValue ? monthPickerValue.format('YYYY 年 M 月') : '全部时间' }} · 全公司机电材料采购概览
        </div>
      </div>
      <div class="dashboard__actions">
        <a-month-picker v-model:value="monthPickerValue" placeholder="全部时间" allow-clear />
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

    <!-- 热力图 + 气泡图 左右并排 -->
    <a-row :gutter="16" class="mb-16">
      <a-col :xs="24" :lg="12">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <span style="font-size:15px;font-weight:600">项目热力图</span>
            <span style="font-size:12px;color:rgba(0,0,0,0.45);margin-left:8px">项目 → 品类 → 金额</span>
          </template>
          <TreeHeatmap :data="heatmapNodes" :loading="loading" />
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="12">
        <a-card :body-style="{ padding: '16px 20px' }">
          <template #title>
            <span style="font-size:15px;font-weight:600">品类气泡图</span>
            <span style="font-size:12px;color:rgba(0,0,0,0.45);margin-left:8px">品类 → 供应商 → 金额</span>
          </template>
          <BubbleChart :data="bubbleItems.map(b => ({ name: b.name, profession: b.profession as '电气'|'给排水'|'暖通', amount: b.total_amount }))" :loading="loading" />
        </a-card>
      </a-col>
    </a-row>

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
