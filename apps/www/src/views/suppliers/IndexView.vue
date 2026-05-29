<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from 'vue'
import {
  TeamOutlined,
  TrophyOutlined,
  PlusOutlined,
  ExportOutlined,
  ImportOutlined,
  FireOutlined,
} from '@ant-design/icons-vue'
import StatCard from '@/components/StatCard.vue'
import { supplierApi, analysisApi, exportApi } from '@/api'
import type { Supplier, SupplierScore } from '@/api/client'
import { doExport } from '@/utils/download'

interface SupplierRow extends Supplier {
  ai_score?: number
  history_deviation?: number  // 历史均价相对值
  delivery_score?: number     // 履约评分
  last_cooperation?: string
  tags?: { label: string; color: string }[]
}

const data = ref<SupplierRow[]>([])
const total = ref(0)
const loading = ref(false)
const isMockData = ref(false)

const query = reactive({
  page: 1,
  page_size: 20,
  category: undefined as string | undefined,
  score_range: undefined as string | undefined,
  freq: undefined as string | undefined,
  region: undefined as string | undefined,
  keyword: undefined as string | undefined,
})

// ─── 模拟数据（后端无数据时使用）─────────────────────────────────────────
const MOCK: SupplierRow[] = [
  { id: 1, name: '江苏华润管业', short_name: '华润', contact: '', phone: '', categories: ['桥架类'], win_count: 8, cooperation_score: 92, remark: '', created_at: null, updated_at: null, history_deviation: -0.052, delivery_score: 0.95, ai_score: 92, last_cooperation: '2026-04-15', tags: [{ label: '价格优势', color: 'green' }, { label: '长期合作', color: 'blue' }] },
  { id: 2, name: '天源华威桥架', short_name: '天源', contact: '', phone: '', categories: ['桥架类','防火桥架'], win_count: 6, cooperation_score: 88, remark: '', created_at: null, updated_at: null, history_deviation: 0.018, delivery_score: 0.98, ai_score: 88, last_cooperation: '2026-04-08', tags: [{ label: '质量优秀', color: 'cyan' }] },
  { id: 3, name: '上海管业贸易', short_name: '上海管业', contact: '', phone: '', categories: ['管材/桥架'], win_count: 3, cooperation_score: 86, remark: '', created_at: null, updated_at: null, history_deviation: -0.075, delivery_score: 0.90, ai_score: 86, last_cooperation: '2026-03-22', tags: [{ label: '价格优势', color: 'green' }, { label: '新合作', color: 'purple' }] },
  { id: 4, name: '广东联墅供应', short_name: '联墅', contact: '', phone: '', categories: ['管材类'], win_count: 4, cooperation_score: 81, remark: '', created_at: null, updated_at: null, history_deviation: -0.036, delivery_score: 0.88, ai_score: 81, last_cooperation: '2026-03-10', tags: [{ label: '稳定供应', color: 'blue' }] },
  { id: 5, name: '江苏华润电气', short_name: '华润电', contact: '', phone: '', categories: ['电气/防火桥架'], win_count: 5, cooperation_score: 78, remark: '', created_at: null, updated_at: null, history_deviation: 0.024, delivery_score: 0.85, ai_score: 78, last_cooperation: '2026-02-28', tags: [{ label: '防火专项', color: 'orange' }] },
  { id: 6, name: '浙江中铁建材', short_name: '中铁', contact: '', phone: '', categories: ['钢材类'], win_count: 7, cooperation_score: 72, remark: '', created_at: null, updated_at: null, history_deviation: 0.061, delivery_score: 0.82, ai_score: 72, last_cooperation: '2026-02-15', tags: [{ label: '需关注', color: 'orange' }] },
  { id: 7, name: '伟星新材华东', short_name: '伟星', contact: '', phone: '', categories: ['管材类'], win_count: 9, cooperation_score: 90, remark: '', created_at: null, updated_at: null, history_deviation: -0.014, delivery_score: 0.96, ai_score: 90, last_cooperation: '2026-01-30', tags: [{ label: '长期合作', color: 'blue' }] },
  { id: 8, name: '金龙铜管有限', short_name: '金龙', contact: '', phone: '', categories: ['暖通铜管'], win_count: 4, cooperation_score: 85, remark: '', created_at: null, updated_at: null, history_deviation: -0.028, delivery_score: 0.91, ai_score: 85, last_cooperation: '2026-01-12', tags: [{ label: '稳定供应', color: 'cyan' }] },
  { id: 9, name: '鞍钢华东仓储', short_name: '鞍钢', contact: '', phone: '', categories: ['钢材类'], win_count: 11, cooperation_score: 87, remark: '', created_at: null, updated_at: null, history_deviation: -0.005, delivery_score: 0.93, ai_score: 87, last_cooperation: '2025-12-28', tags: [{ label: '长期合作', color: 'blue' }] },
  { id: 10, name: '正泰电器华南', short_name: '正泰', contact: '', phone: '', categories: ['配电设备'], win_count: 2, cooperation_score: 65, remark: '', created_at: null, updated_at: null, history_deviation: 0.035, delivery_score: 0.78, ai_score: 65, last_cooperation: '2025-12-10', tags: [{ label: '待评估', color: 'default' }] },
]

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await supplierApi.list(query as Record<string, unknown>)
    if (resp.items.length > 0) {
      data.value = resp.items as SupplierRow[]
      total.value = resp.total
      isMockData.value = false
    } else {
      data.value = MOCK
      total.value = MOCK.length
      isMockData.value = true
    }
  } catch {
    data.value = MOCK
    total.value = MOCK.length
    isMockData.value = true
  } finally {
    loading.value = false
  }
}

// ─── Stats ──────────────────────────────────────────────────────────────
const stats = computed(() => {
  const list = data.value
  return {
    totalCount: total.value,
    aiScoreHigh: list.filter((s) => (s.ai_score ?? 0) >= 85).length,
    newThisYear: isMockData.value ? 5 : list.filter((s) => s.last_cooperation?.startsWith('2026')).length,
    highFreq: list.filter((s) => s.win_count >= 5).length,
  }
})

// ─── Columns ────────────────────────────────────────────────────────────
const columns = [
  { title: '供应商名称', dataIndex: 'name', width: 160, ellipsis: true },
  { title: '类型', dataIndex: 'supplier_type', width: 90, align: 'center' as const },
  { title: '主营物料类别', dataIndex: 'categories', width: 150, ellipsis: true,
    customRender: ({ text }: { text: string[] }) => text?.join('、') || '—' },
  { title: '合作次数', dataIndex: 'win_count', width: 90, align: 'center' as const,
    customRender: ({ text }: { text: number }) => `${text} 次` },
  { title: '历史均价偏差', dataIndex: 'history_deviation', width: 130, align: 'right' as const },
  { title: '履约评分', dataIndex: 'delivery_score', width: 90, align: 'center' as const,
    customRender: ({ text }: { text: number }) => `${Math.round((text ?? 0) * 100)}%` },
  { title: 'AI 综合评分', dataIndex: 'ai_score', width: 110, align: 'center' as const },
  { title: '标签', dataIndex: 'tags', width: 180 },
  { title: '最近合作', dataIndex: 'last_cooperation', width: 110 },
  { title: '操作', key: 'action', width: 90, fixed: 'right' as const },
]

// ─── Drawer (画像) ─────────────────────────────────────────────────────
const drawerVisible = ref(false)
const drawerSupplier = ref<SupplierRow | null>(null)
const drawerScore = ref<SupplierScore | null>(null)
const drawerLoading = ref(false)

async function openProfile(record: SupplierRow) {
  drawerSupplier.value = record
  drawerVisible.value = true
  drawerLoading.value = true
  try {
    const { data } = await analysisApi.supplierScore({ supplier_id: record.id })
    drawerScore.value = data
  } catch {
    // 后端无数据时拼装一份模拟评分
    drawerScore.value = {
      supplier_id: record.id,
      supplier_name: record.name,
      price_score: Math.round(85 + (record.history_deviation ?? 0) * -200),
      history_score: Math.min(100, 40 + record.win_count * 8),
      completeness_score: Math.round((record.delivery_score ?? 0.9) * 100),
      brand_score: 85,
      commercial_score: 75,
      total_score: record.ai_score ?? 80,
      weights: { price: 0.4, history: 0.2, completeness: 0.15, brand: 0.15, commercial: 0.1 },
    }
  } finally {
    drawerLoading.value = false
  }
}

function tagColor(t: { color?: string }) {
  return t.color || 'default'
}

function getScoreColor(score: number) {
  if (score >= 85) return '#52c41a'
  if (score >= 75) return '#1677ff'
  if (score >= 60) return '#faad14'
  return '#ff4d4f'
}

function getDeviationColor(d?: number) {
  if (d === undefined) return 'rgba(0,0,0,0.45)'
  if (d <= -0.03) return '#52c41a'
  if (d >= 0.03) return '#faad14'
  return 'rgba(0,0,0,0.65)'
}

function fmtDeviation(d?: number) {
  if (d === undefined) return '—'
  const pct = (d * 100).toFixed(1)
  return d >= 0 ? `+${pct}%` : `${pct}%`
}

// Reset page on any filter change
watch(
  () => [query.category, query.score_range, query.freq, query.region],
  () => { query.page = 1; fetchData() },
)

onMounted(fetchData)
</script>

<template>
  <div class="suppliers-page">
    <!-- 标题 -->
    <div class="suppliers-page__header">
      <div>
        <h1 class="suppliers-page__title">
          供应商管理
          <a-tag v-if="isMockData" color="orange" style="font-size:11px;margin-left:8px;vertical-align:middle">示例数据</a-tag>
        </h1>
        <div class="suppliers-page__subtitle">供应商档案 · 历史合作沉淀 · 五维评分体系</div>
      </div>
      <div class="flex gap-8">
        <a-button>
          <template #icon><ImportOutlined /></template>
          批量导入
        </a-button>
        <a-button @click="doExport(() => exportApi.suppliers(), 'MEMPAS_供应商名单.xlsx')">
          <template #icon><ExportOutlined /></template>
          导出名单
        </a-button>
        <a-button type="primary">
          <template #icon><PlusOutlined /></template>
          新增供应商
        </a-button>
      </div>
    </div>

    <!-- 摘要 -->
    <a-row :gutter="16" class="mb-16">
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="TeamOutlined"
          icon-bg="rgba(22,119,255,0.1)"
          label="供应商总数"
          :value="stats.totalCount"
          unit="家"
          :trend="{ value: '含活跃 + 待评估', label: '', positive: true }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="TrophyOutlined"
          icon-bg="rgba(82,196,26,0.1)"
          label="供应商管理"
          :value="stats.aiScoreHigh"
          unit="家"
          :trend="{ value: 'AI 综合评分 ≥ 85', label: '', positive: true }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="PlusOutlined"
          icon-bg="rgba(250,173,20,0.1)"
          label="本年新增"
          :value="stats.newThisYear"
          unit="家"
          :trend="{ value: '2026 年度', label: '', positive: true }"
        />
      </a-col>
      <a-col :xs="24" :sm="12" :md="6">
        <StatCard
          :icon="FireOutlined"
          icon-bg="rgba(255,77,79,0.1)"
          label="高频合作"
          :value="stats.highFreq"
          unit="家"
          :trend="{ value: '中标 ≥ 5 次', label: '', positive: true }"
        />
      </a-col>
    </a-row>

    <!-- 筛选 -->
    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space :wrap="true">
        <a-select v-model:value="query.category" placeholder="全部物料类别" allow-clear style="width:140px">
          <a-select-option value="桥架类">桥架类</a-select-option>
          <a-select-option value="管材类">管材类</a-select-option>
          <a-select-option value="电气类">电气类</a-select-option>
          <a-select-option value="暖通类">暖通类</a-select-option>
        </a-select>
        <a-select v-model:value="query.score_range" placeholder="全部评分" allow-clear style="width:120px">
          <a-select-option value="85+">85 分以上</a-select-option>
          <a-select-option value="70-85">70 - 85 分</a-select-option>
          <a-select-option value="<70">70 分以下</a-select-option>
        </a-select>
        <a-select v-model:value="query.freq" placeholder="合作频次" allow-clear style="width:120px">
          <a-select-option value="high">高频 (≥5)</a-select-option>
          <a-select-option value="medium">中频 (2-4)</a-select-option>
          <a-select-option value="low">低频 (&lt;2)</a-select-option>
        </a-select>
        <a-select v-model:value="query.region" placeholder="全部地区" allow-clear style="width:120px">
          <a-select-option value="华东">华东</a-select-option>
          <a-select-option value="华南">华南</a-select-option>
          <a-select-option value="华北">华北</a-select-option>
        </a-select>
        <a-input-search
          v-model:value="query.keyword"
          placeholder="搜索供应商名称..."
          style="width:240px"
          @search="() => { query.page = 1; fetchData() }"
        />
      </a-space>
    </a-card>

    <!-- 表格 -->
    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :loading="loading"
        :pagination="{
          current: query.page,
          pageSize: query.page_size,
          total,
          showSizeChanger: true,
          showTotal: (t: number) => `共 ${t} 家`,
        }"
        :scroll="{ x: 1300 }"
        row-key="id"
        size="middle"
        @change="(pag: any) => { query.page = pag.current; query.page_size = pag.pageSize; fetchData() }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'name'">
            <span style="font-weight:500;color:#1677ff">{{ (record as SupplierRow).name }}</span>
          </template>
          <template v-else-if="column.dataIndex === 'supplier_type'">
            <a-tag :color="(record as SupplierRow).supplier_type === '厂家' ? 'purple' : 'blue'">
              {{ (record as SupplierRow).supplier_type || '供应商' }}
            </a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'history_deviation'">
            <span :style="{ color: getDeviationColor((record as SupplierRow).history_deviation) }">
              {{ fmtDeviation((record as SupplierRow).history_deviation) }}
            </span>
          </template>
          <template v-else-if="column.dataIndex === 'ai_score'">
            <span :style="{ color: getScoreColor((record as SupplierRow).ai_score ?? 0), fontWeight: 600, fontSize: '15px' }">
              {{ (record as SupplierRow).ai_score ?? '—' }}
            </span>
          </template>
          <template v-else-if="column.dataIndex === 'tags'">
            <a-tag
              v-for="t in (record as SupplierRow).tags ?? []"
              :key="t.label"
              :color="tagColor(t)"
            >{{ t.label }}</a-tag>
          </template>
          <template v-else-if="column.key === 'action'">
            <a @click="openProfile(record as SupplierRow)">画像</a>
          </template>
        </template>
      </a-table>
    </a-card>

    <!-- 画像 Drawer -->
    <a-drawer
      v-model:open="drawerVisible"
      :title="drawerSupplier ? `${drawerSupplier.name} · 供应商画像` : '供应商画像'"
      :width="520"
      placement="right"
    >
      <a-spin :spinning="drawerLoading">
        <template v-if="drawerSupplier && drawerScore">
          <a-descriptions :column="2" bordered size="small">
            <a-descriptions-item label="类型">{{ drawerSupplier.supplier_type || '供应商' }}</a-descriptions-item>
            <a-descriptions-item label="主营品类">
              {{ drawerSupplier.categories?.join('、') || '—' }}
            </a-descriptions-item>
            <a-descriptions-item label="中标次数">{{ drawerSupplier.win_count }} 次</a-descriptions-item>
            <a-descriptions-item label="最近合作">{{ drawerSupplier.last_cooperation || '—' }}</a-descriptions-item>
            <a-descriptions-item label="AI 综合评分" :span="2">
              <span :style="{ color: getScoreColor(drawerScore.total_score), fontSize: '22px', fontWeight: 700 }">
                {{ drawerScore.total_score }}
              </span>
              <span style="margin-left:8px;color:rgba(0,0,0,0.45)">/ 100</span>
            </a-descriptions-item>
          </a-descriptions>

          <h3 style="margin: 18px 0 8px; font-size:14px; font-weight:600">五维评分</h3>
          <div class="score-bars">
            <div class="score-bar">
              <span class="score-bar__label">价格竞争力 (40%)</span>
              <a-progress :percent="drawerScore.price_score" :stroke-color="getScoreColor(drawerScore.price_score)" />
            </div>
            <div class="score-bar">
              <span class="score-bar__label">历史合作 (20%)</span>
              <a-progress :percent="drawerScore.history_score" :stroke-color="getScoreColor(drawerScore.history_score)" />
            </div>
            <div class="score-bar">
              <span class="score-bar__label">报价完整度 (15%)</span>
              <a-progress :percent="drawerScore.completeness_score" :stroke-color="getScoreColor(drawerScore.completeness_score)" />
            </div>
            <div class="score-bar">
              <span class="score-bar__label">品牌合规 (15%)</span>
              <a-progress :percent="drawerScore.brand_score" :stroke-color="getScoreColor(drawerScore.brand_score)" />
            </div>
            <div class="score-bar">
              <span class="score-bar__label">商务条款 (10%)</span>
              <a-progress :percent="drawerScore.commercial_score" :stroke-color="getScoreColor(drawerScore.commercial_score)" />
            </div>
          </div>
        </template>
      </a-spin>
    </a-drawer>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.suppliers-page {
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

.score-bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.score-bar {
  &__label {
    display: block;
    font-size: 12px;
    color: @text-color-secondary;
    margin-bottom: 2px;
  }
}
</style>
