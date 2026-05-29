<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import {
  SyncOutlined, CheckCircleOutlined, CloseCircleOutlined,
  ClockCircleOutlined, ReloadOutlined, FileTextOutlined,
} from '@ant-design/icons-vue'
import { intakeApi } from '@/api'
import type { ExtractionJob, JobStatus } from '@/api/client'

const data = ref<ExtractionJob[]>([])
const total = ref(0)
const loading = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

const query = reactive({
  status: undefined as string | undefined,
  type: undefined as string | undefined,
  limit: 50,
})

async function fetchData() {
  loading.value = true
  try {
    const { data: resp } = await intakeApi.listJobs(query as Record<string, unknown>)
    data.value = resp.items
    total.value = resp.total
  } catch {
    data.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

// Summary stats
const stats = computed(() => {
  const pending = data.value.filter(j => j.status === 'pending').length
  const running = data.value.filter(j => j.status === 'running').length
  const done = data.value.filter(j => j.status === 'done').length
  const failed = data.value.filter(j => j.status === 'failed').length
  return { pending, running, done, failed }
})

const columns = [
  { title: '文件名', dataIndex: 'filename', ellipsis: true },
  { title: '类型', dataIndex: 'type', width: 80, align: 'center' as const },
  { title: '状态', dataIndex: 'status', width: 100, align: 'center' as const },
  { title: '进度', dataIndex: 'progress_pct', width: 200 },
  { title: '大小', dataIndex: 'file_size', width: 80, align: 'right' as const },
  { title: '识别条数', dataIndex: 'result_count', width: 90, align: 'right' as const },
  { title: '耗时', dataIndex: 'duration_ms', width: 80, align: 'right' as const },
  { title: 'Tokens', dataIndex: 'tokens_used', width: 80, align: 'right' as const },
  { title: '提交时间', dataIndex: 'created_at', width: 160 },
]

function statusBadge(s: JobStatus) {
  const map: Record<JobStatus, { status: string; text: string }> = {
    pending: { status: 'default', text: '排队中' },
    running: { status: 'processing', text: '识别中' },
    done: { status: 'success', text: '已完成' },
    failed: { status: 'error', text: '失败' },
  }
  return map[s] || { status: 'default', text: s }
}

function typeLabel(t: string) {
  return t === 'tender' ? '招标' : '报价'
}

function fmtDate(d: string | null) {
  if (!d) return '—'
  return d.replace('T', ' ').slice(0, 16)
}

function fmtDuration(ms: number) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function fmtSize(bytes: number) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}

function getResultCount(job: ExtractionJob): string {
  if (job.status !== 'done' || !job.result) return '—'
  const r = job.result as Record<string, unknown>
  const items = r.items as unknown[]
  if (Array.isArray(items)) return String(items.length)
  return '—'
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    if (data.value.some((j) => j.status === 'pending' || j.status === 'running')) {
      fetchData()
    }
  }, 5000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

watch(
  () => [query.status, query.type],
  () => fetchData(),
)

onMounted(() => {
  fetchData()
  startPolling()
})
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="queue-page">
    <div class="queue-page__header">
      <div>
        <h1 class="queue-page__title">数据流</h1>
        <div class="queue-page__subtitle">文档识别任务队列 · 查看每份清单的处理状态</div>
      </div>
      <a-button @click="fetchData" :loading="loading">
        <template #icon><ReloadOutlined /></template>
        刷新
      </a-button>
    </div>

    <!-- Summary cards -->
    <div class="queue-stats">
      <div class="queue-stat-card" @click="query.status = undefined">
        <FileTextOutlined style="font-size:20px; color:#1677ff" />
        <div class="queue-stat-card__value">{{ total }}</div>
        <div class="queue-stat-card__label">全部</div>
      </div>
      <div class="queue-stat-card" @click="query.status = 'pending'">
        <ClockCircleOutlined style="font-size:20px; color:#faad14" />
        <div class="queue-stat-card__value">{{ stats.pending }}</div>
        <div class="queue-stat-card__label">排队中</div>
      </div>
      <div class="queue-stat-card" @click="query.status = 'running'">
        <SyncOutlined style="font-size:20px; color:#1677ff" spin />
        <div class="queue-stat-card__value">{{ stats.running }}</div>
        <div class="queue-stat-card__label">识别中</div>
      </div>
      <div class="queue-stat-card" @click="query.status = 'done'">
        <CheckCircleOutlined style="font-size:20px; color:#52c41a" />
        <div class="queue-stat-card__value">{{ stats.done }}</div>
        <div class="queue-stat-card__label">已完成</div>
      </div>
      <div class="queue-stat-card" @click="query.status = 'failed'">
        <CloseCircleOutlined style="font-size:20px; color:#ff4d4f" />
        <div class="queue-stat-card__value">{{ stats.failed }}</div>
        <div class="queue-stat-card__label">失败</div>
      </div>
    </div>

    <a-card :body-style="{ padding: '14px 16px' }" class="mb-16">
      <a-space :wrap="true">
        <a-select v-model:value="query.status" placeholder="全部状态" allow-clear style="width:130px">
          <a-select-option value="pending">排队中</a-select-option>
          <a-select-option value="running">识别中</a-select-option>
          <a-select-option value="done">已完成</a-select-option>
          <a-select-option value="failed">识别失败</a-select-option>
        </a-select>
        <a-select v-model:value="query.type" placeholder="全部类型" allow-clear style="width:130px">
          <a-select-option value="tender">招标清单</a-select-option>
          <a-select-option value="quote">报价单</a-select-option>
        </a-select>
        <span style="color:rgba(0,0,0,0.45);font-size:12px">
          {{ query.status || query.type ? '筛选结果：' : '' }}{{ data.length }} / {{ total }} 条
        </span>
      </a-space>
    </a-card>

    <a-card :body-style="{ padding: '8px 16px 16px' }">
      <a-table
        :columns="columns"
        :data-source="data"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 1100 }"
        row-key="id"
        size="middle"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.dataIndex === 'type'">
            <a-tag :color="(record as ExtractionJob).type === 'tender' ? 'blue' : 'green'" size="small">
              {{ typeLabel((record as ExtractionJob).type) }}
            </a-tag>
          </template>
          <template v-else-if="column.dataIndex === 'status'">
            <a-badge
              :status="statusBadge((record as ExtractionJob).status).status"
              :text="statusBadge((record as ExtractionJob).status).text"
            />
          </template>
          <template v-else-if="column.dataIndex === 'progress_pct'">
            <template v-if="(record as ExtractionJob).status === 'running'">
              <a-progress
                :percent="(record as ExtractionJob).progress_pct ?? 0"
                :stroke-color="'#1677ff'"
                size="small"
                style="max-width:120px;display:inline-flex;margin-right:8px"
              />
              <span style="font-size:12px;color:rgba(0,0,0,0.45)">{{ (record as ExtractionJob).progress_stage }}</span>
            </template>
            <template v-else-if="(record as ExtractionJob).status === 'done'">
              <a-progress :percent="100" size="small" style="max-width:120px" />
            </template>
            <template v-else-if="(record as ExtractionJob).status === 'failed'">
              <a-tooltip :title="(record as ExtractionJob).error">
                <span style="color:#ff4d4f;font-size:12px">{{ (record as ExtractionJob).error?.slice(0, 30) || '未知错误' }}</span>
              </a-tooltip>
            </template>
            <template v-else>
              <span style="color:rgba(0,0,0,0.25)">等待中</span>
            </template>
          </template>
          <template v-else-if="column.dataIndex === 'file_size'">
            {{ fmtSize((record as ExtractionJob).file_size) }}
          </template>
          <template v-else-if="column.dataIndex === 'result_count'">
            {{ getResultCount(record as ExtractionJob) }}
          </template>
          <template v-else-if="column.dataIndex === 'duration_ms'">
            {{ fmtDuration((record as ExtractionJob).duration_ms) }}
          </template>
          <template v-else-if="column.dataIndex === 'tokens_used'">
            <span v-if="(record as ExtractionJob).tokens_used">
              {{ ((record as ExtractionJob).tokens_used / 1000).toFixed(1) }}k
            </span>
            <span v-else style="color:rgba(0,0,0,0.25)">—</span>
          </template>
          <template v-else-if="column.dataIndex === 'created_at'">
            {{ fmtDate((record as ExtractionJob).created_at) }}
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.queue-page {
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

.queue-stats {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.queue-stat-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 12px 8px;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #f0f0f0;
  cursor: pointer;
  transition: all 0.2s;

  &:hover {
    border-color: @primary-color;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }

  &__value {
    font-size: 20px;
    font-weight: 600;
    color: @heading-color;
    line-height: 1;
  }

  &__label {
    font-size: 12px;
    color: @text-color-secondary;
  }
}

.mb-16 {
  margin-bottom: 16px;
}
</style>
