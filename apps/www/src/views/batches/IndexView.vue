<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { DeleteOutlined } from '@ant-design/icons-vue'
import { message, Modal } from 'ant-design-vue'
import { quoteApi } from '@/api'

interface BatchRow {
  batch_id: string
  count: number
  created_at: string | null
  supplier_id: number | null
  supplier_name: string
  project_id: number | null
  project_name: string
}

const data = ref<BatchRow[]>([])
const total = ref(0)
const loading = ref(false)

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await quoteApi.batches()
    data.value = resp.items
    total.value = resp.total
  } catch {
    data.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

const columns = [
  { title: '批次编号', dataIndex: 'batch_id', width: 220, ellipsis: true },
  { title: '报价条数', dataIndex: 'count', width: 100, align: 'center' as const },
  { title: '供应商', dataIndex: 'supplier_name', width: 200, ellipsis: true },
  { title: '所属项目', dataIndex: 'project_name', ellipsis: true },
  { title: '入库时间', dataIndex: 'created_at', width: 170 },
  { title: '操作', key: 'action', width: 80, fixed: 'right' as const },
]

function fmtDate(d: string | null) {
  if (!d) return '—'
  return d.replace('T', ' ').slice(0, 19)
}

function handleDelete(record: BatchRow) {
  Modal.confirm({
    title: '确认删除',
    content: `确定要删除批次「${record.batch_id}」的 ${record.count} 条报价记录吗？删除后不可恢复。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await quoteApi.deleteBatch(record.batch_id)
        message.success(`已删除 ${record.count} 条报价`)
        fetchData()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '删除失败')
      }
    },
  })
}

onMounted(fetchData)
</script>

<template>
  <div class="batches-page">
    <div class="batches-page__header">
      <div>
        <h1 class="batches-page__title">清单管理</h1>
        <div class="batches-page__subtitle">按上传批次管理已入库的报价清单</div>
      </div>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <span style="color:rgba(0,0,0,0.45);font-size:12px">共 {{ total }} 个批次</span>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 900 }"
        row-key="batch_id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'batch_id'">
            <span style="font-family:monospace;font-size:12px">{{ (record as BatchRow).batch_id }}</span>
          </template>
          <template v-else-if="column.dataIndex === 'count'">
            <a-tag color="blue">{{ (record as BatchRow).count }} 条</a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'supplier_name'">
            {{ (record as BatchRow).supplier_name || '—' }}
          </template>
          <template v-else-if="column.dataIndex === 'project_name'">
            {{ (record as BatchRow).project_name || '—' }}
          </template>
          <template v-else-if="column.dataIndex === 'created_at'">
            {{ fmtDate((record as BatchRow).created_at) }}
          </template>
          <template v-else-if="column.key === 'action'">
            <a style="color:#ff4d4f" @click="handleDelete(record as BatchRow)">
              <DeleteOutlined /> 删除
            </a>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.batches-page {
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
