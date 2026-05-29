<script setup lang="ts">
import { ref, computed, onBeforeUnmount, watch } from 'vue'
import { message, Upload, type UploadProps } from 'ant-design-vue'
import { InboxOutlined, ScanOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { intakeApi } from '@/api'
import type { ExtractionJob, IngestionType } from '@/api/client'

/**
 * Reusable upload component:
 * 1) drag-drop file
 * 2) POST /api/intake/upload → returns job_id
 * 3) poll GET /api/intake/jobs/{id} every 2s
 * 4) emit `extracted(job)` when status=done; emit `failed(error)` on failure
 *
 * Used by both /compare (per-supplier quote uploads) and /invite (tender upload).
 */

const props = withDefaults(defineProps<{
  type: IngestionType
  context?: Record<string, unknown>
  acceptedTypes?: string // override file MIME types
  hint?: string
  pollIntervalMs?: number
}>(), {
  context: () => ({}),
  acceptedTypes: '.pdf,.png,.jpg,.jpeg',
  pollIntervalMs: 2000,
})

const emit = defineEmits<{
  (e: 'extracted', job: ExtractionJob): void
  (e: 'failed', error: string): void
  (e: 'progress', job: ExtractionJob): void
}>()

const currentJob = ref<ExtractionJob | null>(null)
const fileName = ref<string>('')
const isUploading = ref(false)
const pollTimer = ref<ReturnType<typeof setInterval> | null>(null)
const previewUrl = ref<string | null>(null)
// AUDIT-FIX L1: abort after consecutive poll failures so a stale job ID
// doesn't loop forever every 2s. Counter resets on a successful poll.
const pollFailureCount = ref(0)
const MAX_POLL_FAILURES = 5

const status = computed(() => currentJob.value?.status ?? 'idle')
const isProcessing = computed(
  () => status.value === 'pending' || status.value === 'running' || isUploading.value
)
const isDone = computed(() => status.value === 'done')
const isFailed = computed(() => status.value === 'failed')
const progressStage = computed(() => currentJob.value?.progress_stage || '')
const progressPct = computed(() => {
  if (isUploading.value) return 3
  if (isDone.value) return 100
  if (isFailed.value) return currentJob.value?.progress_pct ?? 0
  return currentJob.value?.progress_pct ?? 0
})
const shouldShowProgress = computed(() => isProcessing.value || (!isDone.value && progressPct.value > 0))

const statusLabel = computed(() => {
  if (isUploading.value) return '上传中...'
  if (progressStage.value) return progressStage.value
  switch (status.value) {
    case 'pending':
      return '排队中...'
    case 'running':
      return '识别中...'
    case 'done':
      return '识别完成'
    case 'failed':
      return '识别失败'
    default:
      return ''
  }
})

const draggerProps = computed<UploadProps>(() => ({
  name: 'file',
  multiple: false,
  accept: props.acceptedTypes,
  showUploadList: false,
  beforeUpload: (file: File) => {
    handleFile(file)
    return false
  },
}))

async function handleFile(file: File) {
  // Clean up previous preview if any
  if (previewUrl.value) {
    URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = null
  }
  if (file.type.startsWith('image/')) {
    previewUrl.value = URL.createObjectURL(file)
  }
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
        const msg = `轮询失败 ${MAX_POLL_FAILURES} 次，已停止；请检查后端服务`
        emit('failed', msg)
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
  // AUDIT-FIX H4: full reset including blob URL + polling state
  stopPolling()
  if (previewUrl.value) {
    URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = null
  }
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

onBeforeUnmount(() => {
  stopPolling()
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})

defineExpose({ retry, currentJob })
</script>

<template>
  <div class="intake-uploader">
    <Upload.Dragger v-bind="draggerProps" class="intake-uploader__dragger">
      <p class="ant-upload-drag-icon">
        <component :is="type === 'tender' ? InboxOutlined : ScanOutlined" />
      </p>
      <p class="ant-upload-text">{{ type === 'tender' ? '上传招标文件' : '上传供应商报价单' }}</p>
      <p class="ant-upload-hint">
        {{ hint || '支持 PDF / PNG / JPG；上传后自动识别为结构化数据' }}
      </p>
    </Upload.Dragger>

    <div v-if="currentJob || isUploading" class="intake-uploader__status">
      <a-spin :spinning="isProcessing" size="small" />
      <div class="intake-uploader__status-body">
        <div class="intake-uploader__status-line">
          <strong>{{ fileName || currentJob?.filename }}</strong>
          <a-tag
            :color="isDone ? 'green' : isFailed ? 'red' : 'blue'"
            style="margin-left:8px"
          >
            {{ statusLabel }}
          </a-tag>
          <a-button
            v-if="isFailed"
            size="small"
            type="link"
            @click="retry"
          >
            <template #icon><ReloadOutlined /></template>
            重新上传
          </a-button>
        </div>
        <div v-if="currentJob?.error" class="intake-uploader__error">
          {{ currentJob.error }}
        </div>
        <div v-else-if="shouldShowProgress" class="intake-uploader__progress">
          <a-progress
            :percent="progressPct"
            :status="isFailed ? 'exception' : isDone ? 'success' : 'active'"
            size="small"
          />
          <div class="intake-uploader__progress-text">
            {{ statusLabel }}
          </div>
        </div>
        <div v-else-if="isDone" class="intake-uploader__meta">
          模型：{{ currentJob?.provider }} · 耗时
          {{ currentJob?.duration_ms ?? 0 }} ms · 用 token
          {{ currentJob?.tokens_used ?? 0 }}
        </div>
      </div>
    </div>

    <div v-if="previewUrl" class="intake-uploader__preview">
      <img :src="previewUrl" alt="预览" />
    </div>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.intake-uploader {
  &__dragger { padding: 8px 0; }

  &__status {
    margin-top: 12px;
    padding: 12px;
    border: 1px solid @border-color-split;
    border-radius: @border-radius-base;
    background: #fafafa;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }

  &__status-body { flex: 1; }
  &__status-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
  &__progress { margin-top: 6px; }
  &__progress-text {
    margin-top: 2px;
    font-size: 12px;
    color: @text-color-secondary;
  }
  &__error { font-size: 12px; color: #ff4d4f; margin-top: 4px; }
  &__meta { font-size: 12px; color: @text-color-secondary; margin-top: 4px; }
  &__preview {
    margin-top: 12px;
    img {
      max-width: 100%;
      max-height: 240px;
      object-fit: contain;
      border: 1px solid @border-color-split;
      border-radius: @border-radius-base;
    }
  }
}
</style>
