<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { SearchOutlined, ExportOutlined } from '@ant-design/icons-vue'
import type { Dayjs } from 'dayjs'
import { logApi, exportApi } from '@/api'
import { doExport } from '@/utils/download'
import type { LogEntry } from '@/api/client'

const loading = ref(false)
const data = ref<LogEntry[]>([])
const total = ref(0)

const query = reactive({
  user: undefined as string | undefined,
  module: undefined as string | undefined,
  action: undefined as string | undefined,
  range: undefined as [Dayjs, Dayjs] | undefined,
  page: 1,
  page_size: 20,
})

const columns = [
  { title: '时间', dataIndex: 'time', width: 170 },
  { title: '操作人', dataIndex: 'user', width: 90 },
  { title: '模块', dataIndex: 'module', width: 130 },
  { title: '操作', dataIndex: 'action', width: 130 },
  { title: '对象', dataIndex: 'target', ellipsis: true },
  { title: '结果', dataIndex: 'result', width: 80 },
  { title: '备注', dataIndex: 'remark', ellipsis: true },
]

async function fetchData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: query.page,
      page_size: query.page_size,
    }
    if (query.user) params.user = query.user
    if (query.module) params.module = query.module
    if (query.action) params.action = query.action
    if (query.range?.[0]) params.date_from = query.range[0].format('YYYY-MM-DD')
    if (query.range?.[1]) params.date_to = query.range[1].format('YYYY-MM-DD')

    const { data: resp } = await logApi.list(params)
    data.value = resp.items
    total.value = resp.total
  } catch {
    // interceptor handles notification
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)

function search() {
  query.page = 1
  fetchData()
}
</script>

<template>
  <div class="logs-page">
    <div class="logs-page__header">
      <div>
        <h1 class="logs-page__title">操作日志</h1>
        <div class="logs-page__subtitle">系统全量审计日志 · 用户操作、模块、对象与结果可溯</div>
      </div>
      <a-button @click="doExport(() => exportApi.logs(), 'MEMPAS_操作日志.xlsx')">
        <template #icon><ExportOutlined /></template>
        导出日志
      </a-button>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space wrap>
        <a-input
          v-model:value="query.user"
          placeholder="操作人"
          allow-clear
          style="width:140px"
        />
        <a-select v-model:value="query.module" placeholder="模块" allow-clear style="width:160px">
          <a-select-option value="招标比价分析">招标比价分析</a-select-option>
          <a-select-option value="邀标建议">邀标建议</a-select-option>
          <a-select-option value="物料主数据">物料主数据</a-select-option>
          <a-select-option value="历史价格查询">历史价格查询</a-select-option>
          <a-select-option value="供应商管理">供应商管理</a-select-option>
          <a-select-option value="采购价格导入">采购价格导入</a-select-option>
          <a-select-option value="系统设置">系统设置</a-select-option>
          <a-select-option value="用户管理">用户管理</a-select-option>
        </a-select>
        <a-select v-model:value="query.action" placeholder="操作类型" allow-clear style="width:140px">
          <a-select-option value="导入">导入</a-select-option>
          <a-select-option value="编辑">编辑</a-select-option>
          <a-select-option value="导出">导出</a-select-option>
          <a-select-option value="查询">查询</a-select-option>
          <a-select-option value="新增">新增</a-select-option>
          <a-select-option value="删除">删除</a-select-option>
        </a-select>
        <a-range-picker v-model:value="query.range" />
        <a-button type="primary" @click="search">
          <template #icon><SearchOutlined /></template>
          查询
        </a-button>
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :loading="loading"
        :pagination="{
          current: query.page,
          pageSize: query.page_size,
          total,
          showTotal: (t: number) => `共 ${t} 条`,
          onChange: (p: number) => { query.page = p; fetchData() },
        }"
        row-key="id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'result'">
            <a-tag :color="(record as LogEntry).result === '成功' ? 'green' : 'red'">
              {{ (record as LogEntry).result }}
            </a-tag>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.logs-page {
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
