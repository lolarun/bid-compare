<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  SaveOutlined,
  ThunderboltOutlined,
  CheckOutlined,
  PlusOutlined,
  UserOutlined,
  HistoryOutlined,
  SendOutlined,
  FilePdfOutlined,
  InfoCircleOutlined,
  SlidersOutlined,
} from '@ant-design/icons-vue'
import FileUploadCard from '@/components/FileUploadCard.vue'
import { inviteApi } from '@/api'
import type {
  ExtractionJob,
  TenderExtractionItem,
  SupplierRecommendation,
} from '@/api/client'
import { asTenderBidlistShape } from '@/utils/extraction'

const sourceJob = ref<ExtractionJob | null>(null)
const tenderItems = ref<TenderExtractionItem[]>([])
const projectName = ref('')

const recommending = ref(false)
const recommendations = ref<SupplierRecommendation[]>([])
const categories = ref<string[]>([])
const totalCandidates = ref(0)
const selectedSupplierIds = ref<number[]>([])

const brandRequirements = ref<string[]>([])
const saving = ref(false)
const savedTenderId = ref<number | null>(null)

const hasItems = computed(() => tenderItems.value.length > 0)
const canRecommend = computed(() => hasItems.value && !recommending.value)
const canSave = computed(() => savedTenderId.value === null && selectedSupplierIds.value.length > 0)

// ─── Step 1: ingestion ─────────────────────────────────────────────────────
function onExtracted(job: ExtractionJob) {
  sourceJob.value = job
  const shape = asTenderBidlistShape(job.result)
  tenderItems.value = shape.items
  if (shape.brandRequirements.length > 0) {
    const existing = new Set(brandRequirements.value)
    for (const b of shape.brandRequirements) {
      if (!existing.has(b)) brandRequirements.value.push(b)
    }
  }
  recommendations.value = []
  selectedSupplierIds.value = []
  savedTenderId.value = null
  // Auto-fill project name from recognition if available
  const rawName = (job.result as Record<string, unknown> | null)?.project_name
  if (typeof rawName === 'string' && rawName && !projectName.value) projectName.value = rawName
}

// ─── Step 2: recommendations ───────────────────────────────────────────────
async function generateRecommendations() {
  if (!hasItems.value) {
    message.warning('请先上传招标文件并识别清单')
    return
  }
  recommending.value = true
  try {
    const { data } = await inviteApi.recommend({
      tender_items: tenderItems.value as unknown as Array<Record<string, unknown>>,
      top_n: 5,
      brand_requirements: brandRequirements.value.length > 0 ? brandRequirements.value : undefined,
    })
    recommendations.value = data.recommendations
    categories.value = data.categories
    totalCandidates.value = data.total_candidates
    // 不默认勾选——仅在用户主动选择时才激活
    selectedSupplierIds.value = []
    savedTenderId.value = null
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '推荐失败'
    message.error(detail)
  } finally {
    recommending.value = false
  }
}

watch(tenderItems, () => {
  if (savedTenderId.value !== null) {
    savedTenderId.value = null
    if (recommendations.value.length > 0) message.info('招标清单已修改，请重新生成推荐并保存')
  }
}, { deep: true })

// ─── Step 3: save ───────────────────────────────────────────────────────────
async function saveInvitations() {
  if (selectedSupplierIds.value.length === 0) {
    message.warning('请至少选择 1 家供应商')
    return
  }
  saving.value = true
  try {
    const { data } = await inviteApi.save({
      job_id: sourceJob.value?.id,
      project_name: projectName.value || '未命名招标',
      items: tenderItems.value as unknown as Array<Record<string, unknown>>,
      supplier_ids: selectedSupplierIds.value,
      brand_requirements: brandRequirements.value.length > 0 ? brandRequirements.value : undefined,
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

// ─── Display helpers ──────────────────────────────────────────────────────
// 徽标统一品牌蓝——与原型一致（非彩虹、非紫靛色）
const AVATAR_COLORS = ['#1677ff']
function avatarColor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) & 0xffff
  return AVATAR_COLORS[h % AVATAR_COLORS.length]
}

function priceText(dev: number | null): string {
  if (dev === null) return '—'
  const v = (dev * 100).toFixed(1)
  return dev > 0 ? `+${v}%` : `${v}%`
}

function priceClass(dev: number | null): string {
  if (dev === null) return 'metric-na'
  return dev <= -0.03 ? 'metric-green' : dev >= 0.05 ? 'metric-orange' : 'metric-normal'
}

const TAG_COLORS: Record<string, string> = {
  '价格优势': 'green',
  '长期合作': 'blue',
  '新合作机会': 'cyan',
  '稳定供应': 'geekblue',
  '质量优秀': 'gold',
  '品牌匹配': 'purple',
  '补充邀标': 'default',
}
function tagColor(t: string): string { return TAG_COLORS[t] ?? 'default' }

function matchedBrands(r: SupplierRecommendation): string[] {
  if (!brandRequirements.value.length) return []
  return (r.reason.brands ?? []).filter((b) => brandRequirements.value.includes(b)).slice(0, 3)
}
</script>

<template>
  <div class="invite-page">
    <div class="invite-page__header">
      <div>
        <h1 class="invite-page__title">邀标建议</h1>
        <div class="invite-page__subtitle">
          基于物料、历史价格、供应商画像 · AI 推荐本次招标可邀单位
        </div>
      </div>
      <a-button disabled>
        <template #icon><HistoryOutlined /></template>
        历史邀标
      </a-button>
    </div>

    <a-row :gutter="16" class="invite-page__body">
      <!-- ════════ 左侧：招标信息（容器卡 + 悬浮按钮卡） ════════ -->
      <a-col :xs="24" :lg="8" class="invite-page__col">
        <div class="tender-card">
          <div class="tender-card__title">招标信息</div>
          <div class="tender-card__subtitle">上传招标文件，自动识别清单后生成邀标建议</div>

          <!-- 卡片式上传（点击上传 / 重新上传，传统 File 选择） -->
          <div class="tender-card__upload">
            <FileUploadCard
              :type="'tender_bidlist'"
              placeholder="上传招标文件"
              hint="支持 PDF / 扫描件图片；自动识别招标清单及品牌要求"
              @extracted="onExtracted"
            />
          </div>

          <!-- 识别后：项目名称 → 品牌要求 → 采购清单 -->
          <template v-if="sourceJob">
            <!-- 1. 项目名称 -->
            <div class="field">
              <label class="field__label">项目名称</label>
              <a-input
                v-model:value="projectName"
                placeholder="自动识别，可编辑"
                allow-clear
              />
            </div>

            <!-- 2. 品牌要求 -->
            <div class="field">
              <label class="field__label">品牌要求</label>
              <div v-if="brandRequirements.length" class="brand-tags">
                <a-tag v-for="b in brandRequirements" :key="b" color="purple">{{ b }}</a-tag>
              </div>
              <div v-else class="field__placeholder">未识别到品牌要求</div>
            </div>

            <!-- 3. 采购清单 -->
            <div class="field field--list">
              <label class="field__label">采购清单（{{ tenderItems.length }}项）</label>

              <a-empty v-if="tenderItems.length === 0" description="未识别到材料行" :image-style="{ height: '40px' }" style="padding:12px 0" />
              <div v-else class="item-list">
                <div v-for="(it, idx) in tenderItems" :key="idx" class="item-row">
                  <span class="item-row__name">
                    {{ it.name }}<span v-if="it.spec" class="item-row__spec">（{{ it.spec }}）</span>
                  </span>
                  <span class="item-row__qty">
                    {{ it.quantity ?? '—' }}<span class="item-row__unit"> {{ it.unit }}</span>
                  </span>
                </div>
              </div>
            </div>
          </template>

          <a-empty
            v-else
            description="上传招标文件后，自动识别采购清单与品牌要求"
            style="padding:20px 0 8px"
          />
        </div>

        <!-- 左侧悬浮按钮卡 -->
        <div v-if="sourceJob" class="action-bar action-bar--left">
          <a-button
            type="primary"
            block
            :loading="recommending"
            :disabled="!canRecommend"
            @click="generateRecommendations"
          >
            <template #icon><ThunderboltOutlined /></template>
            生成邀标建议
          </a-button>
        </div>
      </a-col>

      <!-- ════════ 右侧：推荐供应商（无容器 + 悬浮按钮卡） ════════ -->
      <a-col :xs="24" :lg="16" class="invite-page__col">
        <div class="reco-panel">
          <!-- 信息条（常驻） -->
          <div class="reco-header">
            <div class="reco-header__meta">
              <InfoCircleOutlined class="reco-header__icon" />
              <span v-if="recommendations.length && totalCandidates > 0">
                已从优质供应商库中分析 <b>{{ totalCandidates }}</b> 家相关供应商
                · 按 <b>价格优势 60%</b> + <b>履约评分 40%</b> 综合排序
              </span>
              <span v-else>
                上传招标文件后，AI 将从优质供应商库按 <b>价格优势 60%</b> + <b>履约评分 40%</b> 推荐邀标单位
              </span>
            </div>
            <a-button v-if="recommendations.length" type="link" size="small" :loading="recommending" @click="generateRecommendations">
              <template #icon><SlidersOutlined /></template>
              调整权重
            </a-button>
          </div>

          <!-- 供应商卡片列表（无滚动条，随页面滚动） -->
          <div v-if="recommendations.length" class="reco-list">
            <div
              v-for="r in recommendations"
              :key="r.supplier_id"
              class="reco-card"
              :class="{ 'reco-card--selected': selectedSupplierIds.includes(r.supplier_id) }"
            >
              <!-- 左：徽标 + 名称 / 评分 / 指标 -->
              <div class="reco-card__left">
                <div class="reco-card__head">
                  <div class="reco-card__avatar" :style="{ background: avatarColor(r.supplier_name) }">{{ r.supplier_name[0] }}</div>
                  <div class="reco-card__name">{{ r.supplier_name }}</div>
                </div>
                <div class="reco-card__score-block">
                  <span class="reco-card__score-num">{{ r.score.toFixed(0) }}</span>
                  <span class="reco-card__score-label">AI综合评分</span>
                </div>
                <div class="reco-card__metrics">
                  <div class="reco-metric">
                    <div class="reco-metric__label">合作次数</div>
                    <div class="reco-metric__value">{{ r.reason.history_count }} 次</div>
                  </div>
                  <div class="reco-metric">
                    <div class="reco-metric__label">价格优势</div>
                    <div class="reco-metric__value" :class="priceClass(r.reason.avg_deviation_pct)">
                      {{ priceText(r.reason.avg_deviation_pct) }}
                    </div>
                  </div>
                  <div class="reco-metric">
                    <div class="reco-metric__label">按时交付率</div>
                    <div class="reco-metric__value metric-na">—</div>
                  </div>
                </div>
              </div>

              <!-- 中：AI 推荐理由 -->
              <div class="reco-card__main">
                <div class="reco-card__reason-title">
                  <ThunderboltOutlined />
                  AI 推荐理由
                </div>
                <div class="reco-card__reason">{{ r.reason.summary }}</div>

                <div class="reco-card__tags">
                  <a-tag v-for="tag in r.reason.tags" :key="tag" :color="tagColor(tag)" size="small">{{ tag }}</a-tag>
                  <a-tag v-for="b in matchedBrands(r)" :key="'brand-' + b" color="purple" size="small">{{ b }}</a-tag>
                </div>
              </div>

              <!-- 右：操作按钮 -->
              <div class="reco-card__actions">
                <a-button
                  v-if="!selectedSupplierIds.includes(r.supplier_id)"
                  type="primary"
                  size="small"
                  @click="toggleSupplier(r.supplier_id)"
                >
                  <template #icon><PlusOutlined /></template>
                  加入邀标名单
                </a-button>
                <a-button
                  v-else
                  size="small"
                  class="btn-joined"
                  @click="toggleSupplier(r.supplier_id)"
                >
                  <template #icon><CheckOutlined /></template>
                  已加入名单
                </a-button>
                <a-button type="link" size="small" disabled class="btn-profile">
                  <template #icon><UserOutlined /></template>
                  查看画像
                </a-button>
              </div>
            </div>
          </div>

          <!-- 空态 -->
          <div v-else class="reco-empty">
            <a-empty :description="hasItems ? '点击左侧「生成邀标建议」查看推荐供应商' : '上传招标文件后，这里展示推荐供应商'" />
          </div>

          <a-alert
            v-if="savedTenderId !== null"
            type="success"
            show-icon
            :message="`已保存为招标记录 #${savedTenderId}`"
            description="可在「历史邀标」中查看与跟踪状态"
            class="reco-saved"
          />
        </div>

        <!-- 右侧悬浮按钮卡（与左侧一致，sticky 固定在视口底部） -->
        <div v-if="recommendations.length" class="action-bar action-bar--right">
          <span class="action-bar__count">
            已选邀标供应商：<b>{{ selectedSupplierIds.length }}</b> / {{ recommendations.length }}
          </span>
          <a-space>
            <a-button :loading="saving" :disabled="!canSave" @click="saveInvitations">
              <template #icon><SaveOutlined /></template>
              保存为草稿
            </a-button>
            <a-button disabled>
              <template #icon><FilePdfOutlined /></template>
              生成邀标清单 PDF
            </a-button>
            <a-button type="primary" disabled>
              <template #icon><SendOutlined /></template>
              一键发送邀标书
            </a-button>
          </a-space>
        </div>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.invite-page {
  &__header {
    margin-bottom: 16px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
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

// ════════ 左侧招标信息卡 ════════
.tender-card {
  background: @component-background;
  border-radius: @border-radius-lg;
  box-shadow: @shadow-1;
  padding: 16px 18px;

  &__title {
    font-size: 15px;
    font-weight: 600;
    color: @heading-color;
  }
  &__subtitle {
    font-size: 12px;
    color: @text-color-secondary;
    margin: 2px 0 12px;
  }
  &__upload { position: relative; }
}

// 标签一行、组件一行，字段间距稍大
.field {
  margin-top: 20px;

  &__label {
    display: block;
    font-size: 13px;
    color: @text-color;
    margin-bottom: 8px;
  }
  &__placeholder {
    font-size: 13px;
    color: @text-color-tertiary;
  }
}

.brand-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}


// 采购清单：名称（规格）……数量单位（数量右对齐，对齐原型）
.item-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  border-radius: @border-radius-base;
  background: #f7f8fa;
  margin-bottom: 6px;

  &:last-child { margin-bottom: 0; }

  &__name {
    font-size: 13px;
    color: @text-color;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  &__spec { color: @text-color-secondary; }
  &__qty {
    font-size: 13px;
    color: @text-color;
    white-space: nowrap;
    flex-shrink: 0;
  }
  &__unit { color: @text-color-secondary; }
}

// ════════ 右侧面板（无容器卡） ════════
.reco-panel {
  display: flex;
  flex-direction: column;
}

.reco-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  background: #ffffff;
  border: 1px solid #e8e8e8;
  border-radius: @border-radius-base;
  margin-bottom: 12px;

  &__icon {
    color: @primary-color;
    margin-right: 6px;
  }
  &__meta {
    font-size: 13px;
    color: @text-color-secondary;
    b { color: @text-color; font-weight: 600; }
  }
}

.reco-empty {
  padding: 48px 16px;
}

// ════════ 卡片列表 ════════
.reco-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.reco-card {
  display: flex;
  padding: 16px;
  border: 1px solid @border-color-base;
  border-radius: @border-radius-lg;
  background: @component-background;
  box-shadow: @shadow-1;
  transition: border-color 0.15s, box-shadow 0.15s;

  // 激活态：清晰品牌蓝边框（1px 边 + 1px 描边环），无灰色投影干扰
  &--selected {
    border-color: @primary-color;
    box-shadow: 0 0 0 1px @primary-color;
  }

  &__left {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    width: 220px;
    flex-shrink: 0;
    padding-right: 16px;
    border-right: 1px solid @border-color-split;
    margin-right: 16px;
  }

  // 徽标 + 名称（顶部一行，名称紧邻徽标，垂直居中对齐）
  &__head {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    margin-bottom: 12px;
  }

  // 徽标：圆角方形（对齐原型）
  &__avatar {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    color: #fff;
    font-size: 15px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  &__name {
    font-size: 15px;
    font-weight: 600;
    color: @heading-color;
    flex: 1;
    min-width: 0;
    line-height: 1.3;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__score-block {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-bottom: 14px;
  }
  &__score-num {
    font-size: 30px;
    font-weight: 700;
    color: #3fae6e;
    line-height: 1;
  }
  &__score-label {
    font-size: 11px;
    color: @text-color-secondary;
  }
  &__metrics {
    width: 100%;
    display: flex;
    justify-content: space-between;
  }

  // 中：推荐理由
  &__main { flex: 1; min-width: 0; }

  // 右：操作按钮（顶部右对齐）
  &__actions {
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
    margin-left: 16px;
  }

  &__reason-title {
    font-size: 12px;
    font-weight: 600;
    color: @primary-color;
    margin-bottom: 4px;

    .anticon { margin-right: 3px; }
  }
  &__reason {
    font-size: 13px;
    color: @text-color-secondary;
    line-height: 1.6;
    margin-bottom: 8px;
  }

  &__tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
}

.reco-metric {
  text-align: left;

  &__label {
    font-size: 11px;
    color: @text-color-secondary;
    white-space: nowrap;
  }
  &__value {
    font-size: 13px;
    font-weight: 600;
    color: @heading-color;
    line-height: 1.3;
    white-space: nowrap;
    margin-top: 2px;
  }
}

.metric-green  { color: #52c41a; }
.metric-orange { color: #fa8c16; }
.metric-normal { color: @heading-color; }
.metric-na     { color: rgba(0, 0, 0, 0.25); font-weight: 400; }

.btn-joined {
  color: @text-color-secondary;
  border-color: @border-color-base;
  background: @component-background;

  .anticon { color: #52c41a; }
}
.btn-profile {
  height: auto;
  padding: 0;
  font-size: 12px;
}

.reco-saved {
  margin-top: 12px;
}

// ════════ 悬浮按钮卡（左右共用，sticky 固定在视口底部） ════════
.action-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  margin-top: 12px;
  padding: 12px 16px;
  background: @component-background;
  border: 1px solid @border-color-split;
  border-radius: @border-radius-lg;
  box-shadow: 0 -2px 10px rgba(0, 0, 0, 0.07);

  &--right {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__count {
    font-size: 13px;
    color: @text-color-secondary;
    b { color: @heading-color; }
  }
}
</style>
