<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import {
  PlusOutlined,
  HistoryOutlined,
  ExportOutlined,
  ImportOutlined,
  EditOutlined,
  TableOutlined,
  LineChartOutlined,
  AppstoreOutlined,
} from '@ant-design/icons-vue'
import BidMatrix from './components/BidMatrix.vue'

const mode = ref<'matrix' | 'multi' | 'single'>('matrix')

// ─── 当前项目（mock）─────────────────────────────────────────────────────
const currentProject = ref({
  id: 5,
  name: '项目 X 给排水材料采购',
  code: 'BD-2026-0425',
  budget: 260000,
  status: '草稿态',
  bid_date: '2026-04-25',
  material_count: 5,
  invited_suppliers: 4,
})

// ─── 供应商及录入进度（mock）─────────────────────────────────────────────
const suppliers = ref([
  { id: 1, letter: 'A', name: '江苏华润', short: '江苏华润管业', filled: 5, total: 5 },
  { id: 2, letter: 'B', name: '浙江中铁', short: '浙江中铁建材', filled: 3, total: 5 },
  { id: 3, letter: 'C', name: '上海管业', short: '上海管业贸易', filled: 4, total: 5 },
  { id: 4, letter: 'D', name: '广东联墅', short: '广东联墅供应', filled: 2, total: 5 },
])

const activeSupplier = ref(1)

// 当前供应商的报价单
const supplierQuotes = computed(() => [
  { seq: 1, material: 'DN100 无缝钢管', spec: 'Q235', brand: '鞍钢', unit: '米', price: 78.0, qty: 200, status: '已报' },
  { seq: 2, material: '蝶阀 D71X', spec: 'DN150', brand: '良工', unit: '个', price: 720.0, qty: 12, status: '已报' },
  { seq: 3, material: 'PPR 给水管 De32', spec: '3.6mm壁厚', brand: '伟星新材', unit: '米', price: 13.20, qty: 500, status: '已报' },
  { seq: 4, material: '不锈钢水箱', spec: '5m³', brand: '海德隆', unit: '座', price: 8500, qty: 2, status: '已报' },
  { seq: 5, material: 'PE 给水管 De63', spec: 'PN1.6', brand: '联塑', unit: '米', price: 38.0, qty: 280, status: '已报' },
])

const supplierColumns = [
  { title: '序号', dataIndex: 'seq', width: 60 },
  { title: '材料名称', dataIndex: 'material' },
  { title: '规格', dataIndex: 'spec', width: 130 },
  { title: '品牌', dataIndex: 'brand', width: 120 },
  { title: '单位', dataIndex: 'unit', width: 60 },
  { title: '单价', dataIndex: 'price', width: 90, align: 'right' as const, customRender: ({ text }: { text: number }) => text.toFixed(2) },
  { title: '数量', dataIndex: 'qty', width: 80, align: 'right' as const },
  { title: '状态', dataIndex: 'status', width: 70 },
]

// ─── 横向对比矩阵（mock）─────────────────────────────────────────────────
const matrixRows = ref([
  {
    material_id: 101,
    material_name: 'DN100 无缝钢管',
    spec: 'Q235 · 200 米',
    historical_avg: { price: 72.0, period: '2023-01~2024-12', projects: 5 },
    reasonable_low: { price: 65.0, date: '2024-03', project: '华泾镇 D5B' },
    suppliers: [
      { supplier_id: 1, price: 78.0, total: 15600, deviation_pct: 0.20, alert_level: 'red', is_lowest: false },
      { supplier_id: 2, price: 75.0, total: 15000, deviation_pct: 0.154, alert_level: 'red', is_lowest: false },
      { supplier_id: 3, price: 70.0, total: 14000, deviation_pct: 0.077, alert_level: 'yellow', is_lowest: true },
      { supplier_id: 4, price: null, total: null, deviation_pct: null, alert_level: 'normal', is_lowest: false },
    ],
    min_deviation: 0.077,
    recommended: 'C',
  },
  {
    material_id: 102,
    material_name: '蝶阀 D71X',
    spec: 'DN150 · 12 个',
    historical_avg: { price: 760, period: '2023-08~2025-02', projects: 4 },
    reasonable_low: { price: 695, date: '2024-11', project: '前滩 C32' },
    suppliers: [
      { supplier_id: 1, price: 720, total: 8640, deviation_pct: 0.036, alert_level: 'normal', is_lowest: false },
      { supplier_id: 2, price: 705, total: 8460, deviation_pct: 0.014, alert_level: 'normal', is_lowest: true },
      { supplier_id: 3, price: 740, total: 8880, deviation_pct: 0.065, alert_level: 'yellow', is_lowest: false },
      { supplier_id: 4, price: 780, total: 9360, deviation_pct: 0.122, alert_level: 'red', is_lowest: false },
    ],
    min_deviation: 0.014,
    recommended: 'B',
  },
  {
    material_id: 103,
    material_name: 'PPR 给水管 De32',
    spec: '3.6mm壁厚 · 500 米',
    historical_avg: { price: 13.5, period: '2024-01~2025-03', projects: 7 },
    reasonable_low: { price: 12.5, date: '2024-09', project: '虹桥商务区' },
    suppliers: [
      { supplier_id: 1, price: 13.20, total: 6600, deviation_pct: 0.056, alert_level: 'yellow', is_lowest: false },
      { supplier_id: 2, price: 12.80, total: 6400, deviation_pct: 0.024, alert_level: 'normal', is_lowest: true },
      { supplier_id: 3, price: 12.90, total: 6450, deviation_pct: 0.032, alert_level: 'normal', is_lowest: false },
      { supplier_id: 4, price: 13.50, total: 6750, deviation_pct: 0.080, alert_level: 'yellow', is_lowest: false },
    ],
    min_deviation: 0.024,
    recommended: 'B',
  },
  {
    material_id: 104,
    material_name: '不锈钢水箱',
    spec: '5m³ · 2 座',
    historical_avg: { price: 8200, period: '2023-06~2024-10', projects: 3 },
    reasonable_low: { price: 7900, date: '2024-05', project: '前滩 C32' },
    suppliers: [
      { supplier_id: 1, price: 8500, total: 17000, deviation_pct: 0.076, alert_level: 'yellow', is_lowest: true },
      { supplier_id: 2, price: 8800, total: 17600, deviation_pct: 0.114, alert_level: 'red', is_lowest: false },
      { supplier_id: 3, price: null, total: null, deviation_pct: null, alert_level: 'normal', is_lowest: false },
      { supplier_id: 4, price: 9200, total: 18400, deviation_pct: 0.165, alert_level: 'red', is_lowest: false },
    ],
    min_deviation: 0.076,
    recommended: 'A',
  },
  {
    material_id: 105,
    material_name: 'PE 给水管 De63',
    spec: 'PN1.6 · 280 米',
    historical_avg: { price: 40.5, period: '2024-02~2025-04', projects: 6 },
    reasonable_low: { price: 36, date: '2024-08', project: '华润城' },
    suppliers: [
      { supplier_id: 1, price: 38.0, total: 10640, deviation_pct: 0.056, alert_level: 'yellow', is_lowest: true },
      { supplier_id: 2, price: null, total: null, deviation_pct: null, alert_level: 'normal', is_lowest: false },
      { supplier_id: 3, price: 39.0, total: 10920, deviation_pct: 0.083, alert_level: 'yellow', is_lowest: false },
      { supplier_id: 4, price: 41.0, total: 11480, deviation_pct: 0.139, alert_level: 'red', is_lowest: false },
    ],
    min_deviation: 0.056,
    recommended: 'A',
  },
])

const matrixTotals = ref([
  { supplier_id: 1, total: 58480, avg_deviation: 0.085 },
  { supplier_id: 2, total: 47460, avg_deviation: 0.077 },
  { supplier_id: 3, total: 40250, avg_deviation: 0.064 },
  { supplier_id: 4, total: 46000, avg_deviation: 0.142 },
])

const matrixSummary = computed(() => ({
  total_materials: matrixRows.value.length,
  total_suppliers: suppliers.value.length,
  recommended_supplier: { id: 3, name: '上海管业' },
  optimal_total: 53900,
  anomaly_count: matrixRows.value.reduce(
    (s, r) =>
      s +
      r.suppliers.filter(
        (c) => c.alert_level === 'red',
      ).length,
    0,
  ),
}))

const analyzing = ref(false)
async function startAnalysis() {
  analyzing.value = true
  setTimeout(() => {
    analyzing.value = false
    mode.value = 'matrix'
  }, 600)
}

onMounted(() => {
  // 实际接入：调用 POST /api/analysis/bid-matrix
})
</script>

<template>
  <div class="compare-page">
    <!-- 标题区 -->
    <div class="compare-page__header">
      <a-button type="primary">
        <template #icon><PlusOutlined /></template>
        新建比价项目
      </a-button>
      <div class="flex gap-8" style="margin-left:auto">
        <a-button>
          <template #icon><HistoryOutlined /></template>
          历史分析
        </a-button>
        <a-button>
          <template #icon><ExportOutlined /></template>
          导出比价报告（含矩阵）
        </a-button>
      </div>
    </div>

    <!-- 模式切换 -->
    <div class="compare-page__modes">
      <a-segmented v-model:value="mode" :options="[
        { label: '横向对比矩阵', value: 'matrix' },
        { label: '多供应商对比', value: 'multi' },
        { label: '单项价格比对', value: 'single' },
      ]">
        <template #label="{ value, label }">
          <div class="flex gap-8" style="align-items:center">
            <TableOutlined v-if="value === 'matrix'" />
            <AppstoreOutlined v-else-if="value === 'multi'" />
            <LineChartOutlined v-else />
            <span>{{ label }}</span>
          </div>
        </template>
      </a-segmented>
      <span style="margin-left:12px;color:rgba(0,0,0,0.45);font-size:12px">
        默认模式 · 适合一次提交多家投标场景
      </span>
    </div>

    <!-- 项目卡片 -->
    <a-card :body-style="{ padding: '16px 20px' }" class="mb-16">
      <div class="compare-page__project">
        <div>
          <div class="compare-page__project-name">
            {{ currentProject.name }}
            <a-tag style="margin-left:8px">{{ currentProject.status }}</a-tag>
          </div>
          <div class="compare-page__project-meta">
            招标编号 {{ currentProject.code }} · 预算 ¥{{ currentProject.budget.toLocaleString() }}
          </div>
        </div>
        <div class="compare-page__project-stats">
          <div><span class="label">招标日期</span><span class="value">{{ currentProject.bid_date }}</span></div>
          <div><span class="label">材料数量</span><span class="value">{{ currentProject.material_count }} 项</span></div>
          <div><span class="label">邀请供应商</span><span class="value">{{ currentProject.invited_suppliers }} 家</span></div>
        </div>
        <a-space>
          <a-button>
            <template #icon><EditOutlined /></template>
            编辑
          </a-button>
          <a-button>
            <template #icon><ImportOutlined /></template>
            批量导入 Excel
          </a-button>
        </a-space>
      </div>
    </a-card>

    <!-- 供应商进度 -->
    <a-card :body-style="{ padding: '16px 20px' }" class="mb-16">
      <div class="compare-page__suppliers">
        <div
          v-for="s in suppliers"
          :key="s.id"
          class="supplier-chip"
          :class="{ 'supplier-chip--active': s.id === activeSupplier }"
          @click="activeSupplier = s.id"
        >
          <div class="supplier-chip__letter">{{ s.letter }}</div>
          <div class="supplier-chip__body">
            <div class="supplier-chip__name">{{ s.short }}</div>
            <div class="supplier-chip__progress">已录入 {{ s.filled }}/{{ s.total }} 项</div>
          </div>
        </div>
        <a-button class="supplier-chip supplier-chip--add">
          <PlusOutlined />
          <span style="margin-left:6px">添加供应商</span>
        </a-button>
      </div>
    </a-card>

    <!-- 模式切换内容 -->
    <template v-if="mode === 'matrix'">
      <a-card :body-style="{ padding: '16px 20px' }" class="mb-16">
        <template #title>
          <span style="font-size:15px;font-weight:600">{{ currentProject.name }} · 横向对比矩阵</span>
        </template>
        <template #extra>
          <a-space>
            <a-tag color="blue">{{ matrixSummary.total_materials }} 物料</a-tag>
            <a-tag color="purple">{{ matrixSummary.total_suppliers }} 供应商</a-tag>
            <a-tag color="green">推荐 {{ matrixSummary.recommended_supplier.name }}</a-tag>
            <a-tag color="orange">最优总价 ¥{{ matrixSummary.optimal_total.toLocaleString() }}</a-tag>
            <a-tag color="red">异常 {{ matrixSummary.anomaly_count }} 项</a-tag>
          </a-space>
        </template>
        <BidMatrix
          :suppliers="suppliers.map(s => ({ id: s.id, letter: s.letter, name: s.short }))"
          :rows="matrixRows"
          :totals="matrixTotals"
        />
      </a-card>
    </template>

    <template v-else>
      <a-card :body-style="{ padding: '16px 20px' }" class="mb-16">
        <template #title>
          <span style="font-size:15px;font-weight:600">
            {{ suppliers.find(x => x.id === activeSupplier)?.short }} 报价单
          </span>
          <span style="margin-left:12px;font-size:12px;color:rgba(0,0,0,0.45)">
            标签：长期合作 · 报价完整度
          </span>
        </template>
        <template #extra>
          <a-space>
            <a-button size="small">
              <template #icon><ImportOutlined /></template>
              上传报价单
            </a-button>
            <a-button size="small">
              <template #icon><HistoryOutlined /></template>
              从历史复制
            </a-button>
          </a-space>
        </template>
        <a-table
          :columns="supplierColumns"
          :data-source="supplierQuotes"
          :pagination="false"
          row-key="seq"
          size="middle"
        />
      </a-card>
      <a-empty v-if="mode === 'single'" description="单项比对模块（接入 /api/analysis/compare）" />
    </template>

    <!-- 底部 CTA -->
    <div class="compare-page__footer">
      <a-button type="primary" size="large" :loading="analyzing" @click="startAnalysis">
        开始比价分析
      </a-button>
    </div>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.compare-page {
  &__header {
    display: flex;
    align-items: center;
    margin-bottom: 16px;
  }

  &__modes {
    display: flex;
    align-items: center;
    margin-bottom: 16px;
  }

  &__project {
    display: flex;
    align-items: center;
    gap: 24px;
  }

  &__project-name {
    font-size: 15px;
    font-weight: 600;
    color: @heading-color;
  }

  &__project-meta {
    font-size: 12px;
    color: @text-color-tertiary;
    margin-top: 4px;
  }

  &__project-stats {
    display: flex;
    gap: 28px;
    margin-left: auto;
    margin-right: 16px;

    .label {
      display: block;
      font-size: 11px;
      color: @text-color-tertiary;
      margin-bottom: 2px;
    }
    .value {
      font-size: 14px;
      font-weight: 600;
      color: @heading-color;
    }
  }

  &__suppliers {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  &__footer {
    display: flex;
    justify-content: center;
    margin: 24px 0;
  }
}

.supplier-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px 8px 8px;
  border: 1px solid @border-color-base;
  border-radius: 24px;
  cursor: pointer;
  transition: all 0.2s;
  background: #fff;
  min-width: 180px;

  &:hover { border-color: @primary-color; }

  &--active {
    border-color: @primary-color;
    background: @primary-1;
  }

  &--add {
    color: @primary-color;
    border-style: dashed;
    background: transparent;
  }

  &__letter {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: @primary-color;
    color: #fff;
    text-align: center;
    line-height: 24px;
    font-size: 12px;
    font-weight: 600;
    flex-shrink: 0;
  }

  &__body {
    line-height: 1.3;
  }

  &__name {
    font-size: 13px;
    font-weight: 500;
    color: @heading-color;
  }

  &__progress {
    font-size: 11px;
    color: @text-color-tertiary;
  }
}
</style>
