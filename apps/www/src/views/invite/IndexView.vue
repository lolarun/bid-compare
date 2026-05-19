<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  SaveOutlined,
  ThunderboltOutlined,
  TrophyOutlined,
  TeamOutlined,
} from '@ant-design/icons-vue'
import IntakeUploader from '@/components/IntakeUploader.vue'
import ExtractionEditor from '@/components/ExtractionEditor.vue'
import { inviteApi } from '@/api'
import type {
  ExtractionJob,
  TenderExtractionItem,
  SupplierRecommendation,
} from '@/api/client'
import { asTenderShape } from '@/utils/extraction'

/**
 * Invite (邀标建议) page — full real-data flow:
 *
 *   IntakeUploader(tender) → ExtractionEditor(tender items)
 *   → POST /api/invite/recommend → SupplierRecommendation cards
 *   → multi-select + POST /api/invite/save
 */

const sourceJob = ref<ExtractionJob | null>(null)
const tenderItems = ref<TenderExtractionItem[]>([])
const tenderMeta = reactive({
  project_name: '',
  project_code: '',
  tender_date: '',
  deadline: '',
})

const recommending = ref(false)
const recommendations = ref<SupplierRecommendation[]>([])
const categories = ref<string[]>([])
const selectedSupplierIds = ref<number[]>([])
const topN = ref(5)

const saving = ref(false)
const savedTenderId = ref<number | null>(null)

const hasItems = computed(() => tenderItems.value.length > 0)
const canRecommend = computed(() => hasItems.value && !recommending.value)
const canSave = computed(() => savedTenderId.value === null && selectedSupplierIds.value.length > 0)

// ─── Step 1: ingestion result ────────────────────────────────────────────
function onExtracted(job: ExtractionJob) {
  sourceJob.value = job
  // AUDIT-FIX M9: validated coercion via runtime guard, not a raw cast.
  // Contract drift now produces a console.warn rather than silent undefined.
  const shape = asTenderShape(job.result)
  tenderMeta.project_name = shape.project_name
  tenderMeta.project_code = shape.project_code
  tenderMeta.tender_date = shape.tender_date
  tenderMeta.deadline = shape.deadline
  tenderItems.value = shape.items
  // Reset downstream state
  recommendations.value = []
  selectedSupplierIds.value = []
  savedTenderId.value = null
}

function clearAll() {
  sourceJob.value = null
  tenderItems.value = []
  Object.assign(tenderMeta, { project_name: '', project_code: '', tender_date: '', deadline: '' })
  recommendations.value = []
  selectedSupplierIds.value = []
  savedTenderId.value = null
}

// ─── Step 2: generate recommendations ────────────────────────────────────
// AUDIT-FIX H6 + H7: cap default selection at top 3; clear saved-state on
// any subsequent edit so users can't accidentally save stale snapshots.
const DEFAULT_PRESELECT = 3

async function generateRecommendations() {
  if (!hasItems.value) {
    message.warning('请先上传招标文件并核对清单')
    return
  }
  recommending.value = true
  try {
    const { data } = await inviteApi.recommend({
      tender_items: tenderItems.value as unknown as Array<Record<string, unknown>>,
      top_n: topN.value,
    })
    recommendations.value = data.recommendations
    categories.value = data.categories
    // Pre-select only the top 3 (or fewer if top_n is small).
    // Defaulting ALL on with top_n=20 caused accidental mass-invite.
    selectedSupplierIds.value = data.recommendations
      .slice(0, Math.min(DEFAULT_PRESELECT, data.recommendations.length))
      .map((r) => r.supplier_id)
    savedTenderId.value = null  // re-generating = need to save again
    message.success(
      `已生成 ${data.recommendations.length} 家推荐供应商；默认勾选前 ${selectedSupplierIds.value.length} 家`
    )
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '推荐失败'
    message.error(detail)
  } finally {
    recommending.value = false
  }
}

// AUDIT-FIX H7: when the user edits tender items AFTER saving, the saved
// snapshot is now stale. Clear savedTenderId so the user can save again
// with the new state (this re-enables the Save button).
watch(tenderItems, () => {
  if (savedTenderId.value !== null) {
    savedTenderId.value = null
    if (recommendations.value.length > 0) {
      message.info('招标清单已修改，请重新生成推荐并保存')
    }
  }
}, { deep: true })

// ─── Step 3: save invitations ────────────────────────────────────────────
async function saveInvitations() {
  if (selectedSupplierIds.value.length === 0) {
    message.warning('请至少勾选 1 家供应商')
    return
  }
  saving.value = true
  try {
    const { data } = await inviteApi.save({
      job_id: sourceJob.value?.id,
      project_name: tenderMeta.project_name || '未命名招标',
      project_code: tenderMeta.project_code,
      tender_date: tenderMeta.tender_date,
      deadline: tenderMeta.deadline,
      items: tenderItems.value as unknown as Array<Record<string, unknown>>,
      supplier_ids: selectedSupplierIds.value,
    })
    savedTenderId.value = data.tender_id
    message.success(`已保存招标记录 #${data.tender_id}，邀请 ${data.invitations.length} 家供应商`)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '保存失败'
    message.error(detail)
  } finally {
    saving.value = false
  }
}

function toggleSupplier(id: number) {
  const i = selectedSupplierIds.value.indexOf(id)
  if (i >= 0) selectedSupplierIds.value.splice(i, 1)
  else selectedSupplierIds.value.push(id)
}
</script>

<template>
  <div class="invite-page">
    <!-- Page header -->
    <div class="invite-page__header">
      <div>
        <h1 class="invite-page__title">邀标建议</h1>
        <div class="invite-page__subtitle">
          上传招标文件 → 自动识别材料清单 → AI 推荐优秀供应商 → 一键发起邀请
        </div>
      </div>
    </div>

    <a-row :gutter="20">
      <a-col :xs="24" :lg="14">
        <!-- Step 1: upload tender -->
        <a-card title="① 上传招标文件" :body-style="{ padding: '16px 20px' }" style="margin-bottom:16px">
          <template #extra>
            <a-button v-if="sourceJob" size="small" @click="clearAll">重新上传</a-button>
          </template>
          <IntakeUploader
            v-if="!sourceJob"
            :type="'tender'"
            hint="支持招标文件 PDF / 扫描件图片；上传后自动识别项目信息与材料清单"
            @extracted="onExtracted"
          />
          <div v-else>
            <a-descriptions :column="2" size="small" bordered>
              <a-descriptions-item label="项目名称">{{ tenderMeta.project_name || '—' }}</a-descriptions-item>
              <a-descriptions-item label="招标编号">{{ tenderMeta.project_code || '—' }}</a-descriptions-item>
              <a-descriptions-item label="招标日期">{{ tenderMeta.tender_date || '—' }}</a-descriptions-item>
              <a-descriptions-item label="投标截止">{{ tenderMeta.deadline || '—' }}</a-descriptions-item>
            </a-descriptions>
          </div>
        </a-card>

        <!-- Step 2: edit items -->
        <a-card v-if="sourceJob" title="② 核对材料清单" :body-style="{ padding: '16px 20px' }" style="margin-bottom:16px">
          <template #extra>
            <span style="font-size:12px;color:rgba(0,0,0,0.45)">
              共 {{ tenderItems.length }} 项
            </span>
          </template>
          <a-empty v-if="tenderItems.length === 0" description="未识别到材料行；可手动添加" />
          <ExtractionEditor
            v-else
            schema="tender"
            :model-value="tenderItems as unknown[] as any"
            :show-actions="false"
            @update:model-value="(v: any) => tenderItems = v"
          />
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="10">
        <!-- Step 3: generate recommendations -->
        <a-card title="③ 推荐供应商" :body-style="{ padding: '16px 20px' }" style="margin-bottom:16px">
          <template #extra>
            <a-space>
              <span style="font-size:12px">推荐数</span>
              <a-input-number v-model:value="topN" :min="1" :max="20" size="small" style="width:70px" />
            </a-space>
          </template>

          <div v-if="recommendations.length === 0">
            <a-alert
              v-if="!hasItems"
              type="info"
              message="请先在左侧上传并核对招标清单"
              show-icon
              style="margin-bottom:12px"
            />
            <a-button
              type="primary"
              :loading="recommending"
              :disabled="!canRecommend"
              block
              @click="generateRecommendations"
            >
              <template #icon><ThunderboltOutlined /></template>
              生成推荐
            </a-button>
          </div>

          <div v-else>
            <div style="font-size:12px;color:rgba(0,0,0,0.55);margin-bottom:8px">
              针对品类：
              <a-tag v-for="c in categories" :key="c" color="blue">{{ c }}</a-tag>
            </div>

            <div class="reco-list">
              <div
                v-for="r in recommendations"
                :key="r.supplier_id"
                class="reco-card"
                :class="{ 'reco-card--selected': selectedSupplierIds.includes(r.supplier_id) }"
                role="button"
                tabindex="0"
                :aria-pressed="selectedSupplierIds.includes(r.supplier_id)"
                :aria-label="`推荐供应商 ${r.supplier_name}，评分 ${r.score}，排名第 ${r.rank}`"
                @click="toggleSupplier(r.supplier_id)"
                @keydown.enter.prevent="toggleSupplier(r.supplier_id)"
                @keydown.space.prevent="toggleSupplier(r.supplier_id)"
              >
                <div class="reco-card__head">
                  <div class="reco-card__rank">#{{ r.rank }}</div>
                  <div class="reco-card__name">{{ r.supplier_name }}</div>
                  <div class="reco-card__score">
                    <TrophyOutlined />
                    {{ r.score.toFixed(1) }}
                  </div>
                  <a-checkbox
                    :checked="selectedSupplierIds.includes(r.supplier_id)"
                    @click.stop
                    @change="toggleSupplier(r.supplier_id)"
                  />
                </div>
                <div class="reco-card__summary">{{ r.reason.summary }}</div>
                <div class="reco-card__metrics">
                  <a-tag color="blue">
                    <TeamOutlined /> 成交 {{ r.reason.history_count }}
                  </a-tag>
                  <a-tag v-if="r.reason.avg_deviation_pct !== null" :color="r.reason.avg_deviation_pct <= 0 ? 'green' : 'orange'">
                    偏差 {{ (r.reason.avg_deviation_pct * 100).toFixed(1) }}%
                  </a-tag>
                  <a-tag>综合 {{ r.reason.overall_score.toFixed(0) }}</a-tag>
                </div>
              </div>
            </div>

            <div style="display:flex;justify-content:space-between;margin-top:12px">
              <a-button @click="generateRecommendations">重新生成</a-button>
              <a-button
                type="primary"
                :loading="saving"
                :disabled="!canSave"
                @click="saveInvitations"
              >
                <template #icon><SaveOutlined /></template>
                保存邀请名单（{{ selectedSupplierIds.length }}）
              </a-button>
            </div>

            <a-alert
              v-if="savedTenderId !== null"
              type="success"
              show-icon
              :message="`已保存为招标记录 #${savedTenderId}`"
              description="可在「邀标历史」中查看与跟踪状态"
              style="margin-top:12px"
            />
          </div>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.invite-page {
  &__header { margin-bottom: 16px; }
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

.reco-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 540px;
  overflow-y: auto;
}

.reco-card {
  padding: 10px 12px;
  border: 1px solid @border-color-base;
  border-radius: @border-radius-base;
  cursor: pointer;
  transition: all 0.2s;
  background: #fff;

  &:hover { border-color: @primary-color; }
  &--selected {
    border-color: @primary-color;
    background: @primary-1;
  }

  &__head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  &__rank {
    background: @primary-color;
    color: #fff;
    font-size: 12px;
    padding: 2px 6px;
    border-radius: 4px;
    flex-shrink: 0;
  }
  &__name {
    font-size: 14px;
    font-weight: 500;
    color: @heading-color;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  &__score {
    font-size: 13px;
    color: @primary-color;
    font-weight: 500;
  }
  &__summary {
    font-size: 12px;
    color: @text-color-secondary;
    margin-bottom: 6px;
    line-height: 1.5;
  }
  &__metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
}
</style>
