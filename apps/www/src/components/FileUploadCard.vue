<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { message } from 'ant-design-vue'
import { UploadOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { intakeApi } from '@/api'
import type { ExtractionJob, IngestionType } from '@/api/client'

/**
 * Card-style uploader (traditional file-picker, no drag-drop):
 *   ┌────────────────────────────────────────────┬──────────────┐
 *   │ 文件名 [已识别]                              │              │
 *   │ 模型 · 耗时 · token / 进度条 / 错误           │  [点击上传]  │
 *   └────────────────────────────────────────────┴──────────────┘
 *
 * 1) click button → native <input type=file>
 * 2) POST /api/intake/upload → job_id
 * 3) poll GET /api/intake/jobs/{id}
 * 4) emit `extracted(job)` on done; `failed(error)` on failure
 *
 * Same upload/poll contract as the previous IntakeUploader (drag-drop).
 */

const props = withDefaults(defineProps<{
  type: IngestionType
  context?: Record<string, unknown>
  acceptedTypes?: string
  hint?: string
  placeholder?: string
  pollIntervalMs?: number
}>(), {
  context: () => ({}),
  acceptedTypes: '.pdf,.png,.jpg,.jpeg',
  hint: '支持 PDF / 扫描件图片；上传后自动识别为结构化数据',
  placeholder: '上传文件',
  pollIntervalMs: 2000,
})

const emit = defineEmits<{
  (e: 'extracted', job: ExtractionJob): void
  (e: 'failed', error: string): void
  (e: 'progress', job: ExtractionJob): void
}>()

const fileInput = ref<HTMLInputElement | null>(null)
const currentJob = ref<ExtractionJob | null>(null)
const fileName = ref<string>('')
const isUploading = ref(false)
const pollTimer = ref<ReturnType<typeof setInterval> | null>(null)
const pollFailureCount = ref(0)
const MAX_POLL_FAILURES = 5

const status = computed(() => currentJob.value?.status ?? 'idle')
const isProcessing = computed(
  () => status.value === 'pending' || status.value === 'running' || isUploading.value,
)
const isDone = computed(() => status.value === 'done')
const isFailed = computed(() => status.value === 'failed')
const hasFile = computed(() => !!currentJob.value || isUploading.value)
const progressStage = computed(() => currentJob.value?.progress_stage || '')
const progressPct = computed(() => {
  if (isUploading.value) return 3
  if (isDone.value) return 100
  return currentJob.value?.progress_pct ?? 0
})
const shouldShowProgress = computed(() => isProcessing.value || (!isDone.value && progressPct.value > 0))

const statusLabel = computed(() => {
  if (isUploading.value) return '上传中...'
  if (progressStage.value) return progressStage.value
  switch (status.value) {
    case 'pending': return '排队中...'
    case 'running': return '识别中...'
    case 'done': return '已识别'
    case 'failed': return '识别失败'
    default: return ''
  }
})

function pick() {
  fileInput.value?.click()
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  // reset so re-picking the same file still fires change
  input.value = ''
  if (file) handleFile(file)
}

async function handleFile(file: File) {
  fileName.value = file.name

  const form = new FormData()
  form.append('file', file)
  form.append('type', props.type)
  for (const [k, v] of Object.entries(props.context)) {
    if (v !== undefined && v !== null && v !== '') {
      form.append(k, String(v))
    }
  }

  isUploading.value = true
  stopPolling()
  try {
    const { data } = await intakeApi.upload(form)
    currentJob.value = data
    isUploading.value = false
    if (data.status === 'done') {
      warnSkippedBatches(data)
      emit('extracted', data)
    } else if (data.status === 'failed') {
      emit('failed', data.error || '未知错误')
    } else {
      startPolling(data.id)
    }
  } catch (e) {
    isUploading.value = false
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? (e as Error).message
    message.error(`上传失败：${detail}`)
    emit('failed', detail || 'upload error')
  }
}

function startPolling(jobId: string) {
  stopPolling()
  pollFailureCount.value = 0
  pollTimer.value = setInterval(async () => {
    try {
      const { data } = await intakeApi.getJob(jobId)
      pollFailureCount.value = 0
      const prevStatus = currentJob.value?.status
      const prevStage = currentJob.value?.progress_stage
      const prevPct = currentJob.value?.progress_pct
      currentJob.value = data
      if (
        data.status !== prevStatus
        || data.progress_stage !== prevStage
        || data.progress_pct !== prevPct
      ) {
        emit('progress', data)
      }
      if (data.status === 'done') {
        stopPolling()
        warnSkippedBatches(data)
        emit('extracted', data)
      } else if (data.status === 'failed') {
        stopPolling()
        emit('failed', data.error || 'extraction failed')
      }
    } catch (e) {
      pollFailureCount.value += 1
      console.warn(`poll failed (${pollFailureCount.value}/${MAX_POLL_FAILURES})`, e)
      if (pollFailureCount.value >= MAX_POLL_FAILURES) {
        stopPolling()
        emit('failed', `轮询失败 ${MAX_POLL_FAILURES} 次，已停止；请检查后端服务`)
      }
    }
  }, props.pollIntervalMs)
}

function stopPolling() {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

function retry() {
  stopPolling()
  currentJob.value = null
  fileName.value = ''
  pollFailureCount.value = 0
}

function warnSkippedBatches(job: ExtractionJob) {
  const meta = (job.result as Record<string, unknown> | null)?.metadata as Record<string, unknown> | undefined
  const skipped = meta?.skipped_batches as string[] | undefined
  if (skipped && skipped.length > 0) {
    message.warning(`有 ${skipped.length} 个页面因内容审核被跳过，请核对是否有数据缺失`, 6)
  }
}

watch(() => props.type, () => retry())

onBeforeUnmount(() => stopPolling())

defineExpose({ retry, currentJob })
</script>

<template>
  <div class="upload-card" :class="{ 'upload-card--done': isDone, 'upload-card--failed': isFailed }">
    <div class="upload-card__info">
      <!-- 未上传：占位标题 + 提示 -->
      <template v-if="!hasFile">
        <div class="upload-card__title">{{ placeholder }}</div>
        <div class="upload-card__hint">{{ hint }}</div>
      </template>

      <!-- 已选文件：文件名 + 状态 + 进度/元信息/错误 -->
      <template v-else>
        <div class="upload-card__line">
          <strong class="upload-card__name">{{ fileName || currentJob?.filename }}</strong>
          <a-tag :color="isDone ? 'green' : isFailed ? 'red' : 'blue'">{{ statusLabel }}</a-tag>
        </div>
        <div v-if="currentJob?.error" class="upload-card__error">{{ currentJob.error }}</div>
        <div v-else-if="shouldShowProgress" class="upload-card__progress">
          <a-progress
            :percent="progressPct"
            :status="isFailed ? 'exception' : isDone ? 'success' : 'active'"
            size="small"
          />
        </div>
        <div v-else-if="isDone" class="upload-card__meta">
          模型：{{ currentJob?.provider }} · 耗时 {{ currentJob?.duration_ms ?? 0 }} ms · 用 token {{ currentJob?.tokens_used ?? 0 }}
        </div>
      </template>
    </div>

    <div class="upload-card__action">
      <a-button
        :type="hasFile ? 'default' : 'primary'"
        :loading="isUploading"
        @click="pick"
      >
        <template #icon><component :is="hasFile ? ReloadOutlined : UploadOutlined" /></template>
        {{ hasFile ? '重新上传' : '点击上传' }}
      </a-button>
    </div>

    <input
      ref="fileInput"
      type="file"
      :accept="acceptedTypes"
      class="upload-card__input"
      @change="onPick"
    />
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.upload-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid @border-color-base;
  border-radius: @border-radius-lg;
  background: @component-background;
  transition: border-color 0.15s;

  &--done { border-color: #b7eb8f; background: #f6ffed; }
  &--failed { border-color: #ffccc7; background: #fff2f0; }

  &__info {
    flex: 1;
    min-width: 0;
  }
  &__title {
    font-size: 14px;
    font-weight: 600;
    color: @heading-color;
  }
  &__hint {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }
  &__line {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  &__name {
    font-size: 14px;
    color: @heading-color;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  &__progress { margin-top: 6px; }
  &__error { font-size: 12px; color: #ff4d4f; margin-top: 4px; }
  &__meta {
    font-size: 12px;
    color: @text-color-secondary;
    margin-top: 4px;
  }

  &__action { flex-shrink: 0; }
  &__input { display: none; }
}
</style>
