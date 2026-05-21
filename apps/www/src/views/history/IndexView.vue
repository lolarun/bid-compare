<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { message } from 'ant-design-vue'
import { ExportOutlined, SearchOutlined, ReloadOutlined, EditOutlined } from '@ant-design/icons-vue'
import type { Dayjs } from 'dayjs'
import { quoteApi, supplierApi, exportApi } from '@/api'
import type { Quote, Supplier } from '@/api/client'
import { normalizeAlert, alertColors, formatDeviation } from '@/utils/alert'
import { doExport } from '@/utils/download'

interface QuoteRow extends Quote {
  material_name?: string
  spec?: string
  supplier_name?: string
  unit?: string
}

const data = ref<QuoteRow[]>([])
const total = ref(0)
const loading = ref(false)
const suppliers = ref<Supplier[]>([])

const query = reactive({
  page: 1,
  page_size: 20,
  profession: undefined as string | undefined,
  keyword: undefined as string | undefined,
  supplier_id: undefined as number | undefined,
  date_range: undefined as [Dayjs, Dayjs] | undefined,
  price_min: undefined as number | undefined,
  price_max: undefined as number | undefined,
})

const PROFESSIONS = ['电气', '给排水', '暖通']

const columns = [
  { title: '编号', dataIndex: 'id', width: 140, customRender: ({ text }: { text: number }) => `MAT-2026-04-${String(text).padStart(4, '0')}` },
  { title: '材料名称', dataIndex: 'material_name', ellipsis: true },
  { title: '规格', dataIndex: 'spec', width: 130, ellipsis: true },
  { title: '品牌', dataIndex: 'brand', width: 100 },
  { title: '供应商', dataIndex: 'supplier_name', width: 140, ellipsis: true },
  { title: '单位', dataIndex: 'unit', width: 60 },
  { title: '单价', dataIndex: 'unit_price', width: 100, align: 'right' as const,
    customRender: ({ text }: { text: number | null }) => text === null ? '—' : `¥${text.toLocaleString()}` },
  { title: '偏差%', dataIndex: 'deviation_pct', width: 90, align: 'right' as const },
  { title: '招标时间', dataIndex: 'quote_date', width: 110 },
  { title: '操作', key: 'action', width: 120, fixed: 'right' as const },
]

async function fetchData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: query.page,
      page_size: query.page_size,
    }
    if (query.profession) params.profession = query.profession
    if (query.keyword) params.keyword = query.keyword
    if (query.supplier_id) params.supplier_id = query.supplier_id
    if (query.date_range && query.date_range[0] && query.date_range[1]) {
      params.date_from = query.date_range[0].format('YYYY-MM-DD')
      params.date_to = query.date_range[1].format('YYYY-MM-DD')
    }
    if (query.price_min !== undefined) params.price_min = query.price_min
    if (query.price_max !== undefined) params.price_max = query.price_max

    const { data: resp } = await quoteApi.list(params)
    data.value = resp.items as QuoteRow[]
    total.value = resp.total
  } catch {
    data.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function fetchSuppliers() {
  try {
    const { data: resp } = await supplierApi.list({ page: 1, page_size: 100 })
    suppliers.value = resp.items
  } catch {
    suppliers.value = []
  }
}

function reset() {
  query.profession = undefined
  query.keyword = undefined
  query.supplier_id = undefined
  query.date_range = undefined
  query.price_min = undefined
  query.price_max = undefined
  query.page = 1
  fetchData()
}

function viewRecord(record: QuoteRow) {
  message.info(`查看记录 #${record.id}（详情页待实现）`)
}

function citeRecord(record: QuoteRow) {
  message.success(`已引用 #${record.id} 作为参考价`)
}

onMounted(() => {
  fetchData()
  fetchSuppliers()
})
</script>

<template>
  <div class="history-page">
    <!-- 标题 -->
    <div class="history-page__header">
      <div>
        <h1 class="history-page__title">采购数据分析</h1>
        <div class="history-page__subtitle">
          历史采购价格 · 偏差分析 · 已收录 <strong>{{ total }}</strong> 条记录
        </div>
      </div>
      <div class="flex gap-8">
        <a-button @click="doExport(() => exportApi.quotes({ category: query.profession, supplier_id: query.supplier_id }), 'MEMPAS_采购数据.xlsx')">
          <template #icon><ExportOutlined /></template>
          导出
        </a-button>
        <a-button>
          <template #icon><EditOutlined /></template>
          批量操作
        </a-button>
      </div>
    </div>

    <!-- 筛选 -->
    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-row :gutter="12" align="middle">
        <a-col :xs="24" :sm="12" :md="4">
          <a-select
            v-model:value="query.profession"
            placeholder="专业类别"
            allow-clear
            style="width:100%"
          >
            <a-select-option v-for="p in PROFESSIONS" :key="p" :value="p">{{ p }}</a-select-option>
          </a-select>
        </a-col>
        <a-col :xs="24" :sm="12" :md="4">
          <a-input
            v-model:value="query.keyword"
            placeholder="输入材料名称或编码"
            allow-clear
          />
        </a-col>
        <a-col :xs="24" :sm="12" :md="4">
          <a-select
            v-model:value="query.supplier_id"
            placeholder="全部供应商"
            allow-clear
            show-search
            :filter-option="(input: string, opt: { label: string }) => (opt.label || '').includes(input)"
            style="width:100%"
          >
            <a-select-option v-for="s in suppliers" :key="s.id" :value="s.id" :label="s.name">
              {{ s.name }}
            </a-select-option>
          </a-select>
        </a-col>
        <a-col :xs="24" :sm="24" :md="5">
          <a-range-picker v-model:value="query.date_range" style="width:100%" />
        </a-col>
        <a-col :xs="24" :sm="24" :md="4">
          <a-input-group compact>
            <a-input-number
              v-model:value="query.price_min"
              placeholder="¥0"
              :min="0"
              style="width:50%"
            />
            <a-input-number
              v-model:value="query.price_max"
              placeholder="¥不限"
              :min="0"
              style="width:50%"
            />
          </a-input-group>
        </a-col>
        <a-col :xs="24" :sm="24" :md="3">
          <a-space style="display:flex;justify-content:flex-end;width:100%">
            <a-button @click="reset">
              <template #icon><ReloadOutlined /></template>
              重置
            </a-button>
            <a-button type="primary" @click="() => { query.page = 1; fetchData() }">
              <template #icon><SearchOutlined /></template>
              查询
            </a-button>
          </a-space>
        </a-col>
      </a-row>
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
          showTotal: (t: number) => `共 ${t} 条`,
        }"
        :scroll="{ x: 1300 }"
        row-key="id"
        size="middle"
        @change="(pag: any) => { query.page = pag.current; query.page_size = pag.pageSize; fetchData() }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'deviation_pct'">
            <span
              v-if="(record as QuoteRow).deviation_pct !== null"
              :style="{ color: alertColors[normalizeAlert((record as QuoteRow).alert_level)] }"
            >
              {{ formatDeviation((record as QuoteRow).deviation_pct) }}
            </span>
            <span v-else style="color:rgba(0,0,0,0.45)">—</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <a-space>
              <a @click="viewRecord(record as QuoteRow)">查看</a>
              <a @click="citeRecord(record as QuoteRow)" style="color:@primary-color">引用</a>
            </a-space>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.history-page {
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
</style>
