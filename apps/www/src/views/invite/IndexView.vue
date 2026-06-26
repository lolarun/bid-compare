<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { message } from 'ant-design-vue'
import {
  SaveOutlined,
  ThunderboltOutlined,
  CheckOutlined,
  PlusOutlined,
  HistoryOutlined,
  SendOutlined,
  FilePdfOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons-vue'
import FileUploadCard from '@/components/FileUploadCard.vue'
import { inviteApi } from '@/api'
import type {
  ExtractionJob,
  TenderExtractionItem,
  BrandRecommendation,
} from '@/api/client'
import { asTenderBidlistShape } from '@/utils/extraction'

const sourceJob = ref<ExtractionJob | null>(null)
const tenderItems = ref<TenderExtractionItem[]>([])
const projectName = ref('')

const recommending = ref(false)
const recommendations = ref<BrandRecommendation[]>([])
const categories = ref<string[]>([])
const selectedBrands = ref<string[]>([])

// 每张卡片高度（含 gap）约 135px；去掉顶导、页头、信息条、分页、底部操作栏等固定开销
const CARD_H = 135
const OVERHEAD = 320
const windowHeight = ref(window.innerHeight)
function _onResize() { windowHeight.value = window.innerHeight }
onMounted(() => window.addEventListener('resize', _onResize))
onUnmounted(() => window.removeEventListener('resize', _onResize))

const pageSize = computed(() => Math.max(3, Math.floor((windowHeight.value - OVERHEAD) / CARD_H)))
const currentPage = ref(1)

// 窗口缩小时防止当前页超出范围
watch(pageSize, (sz) => {
  const maxPage = Math.ceil(recommendations.value.length / sz)
  if (currentPage.value > maxPage) currentPage.value = Math.max(1, maxPage)
})

const pagedRecs = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return recommendations.value.slice(start, start + pageSize.value)
})

const brandRequirements = ref<string[]>([])
const saving = ref(false)
const savedTenderId = ref<number | null>(null)

const hasItems = computed(() => tenderItems.value.length > 0)
const canRecommend = computed(() => hasItems.value && !recommending.value)
const canSave = computed(() => savedTenderId.value === null && recommendations.value.length > 0)

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
  selectedBrands.value = []
  savedTenderId.value = null
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
      top_n: 100,
      brand_requirements: brandRequirements.value.length > 0 ? brandRequirements.value : undefined,
    })
    recommendations.value = data.recommendations
    categories.value = data.categories
    selectedBrands.value = []
    savedTenderId.value = null
    currentPage.value = 1
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
  saving.value = true
  try {
    const { data } = await inviteApi.save({
      job_id: sourceJob.value?.id,
      project_name: projectName.value || '未命名招标',
      items: tenderItems.value as unknown as Array<Record<string, unknown>>,
      brand_requirements: selectedBrands.value.length > 0
        ? selectedBrands.value
        : brandRequirements.value.length > 0 ? brandRequirements.value : undefined,
    })
    savedTenderId.value = data.tender_id
    message.success(`已保存招标记录 #${data.tender_id}`)
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '保存失败'
    message.error(detail)
  } finally {
    saving.value = false
  }
}

function toggleBrand(name: string) {
  const i = selectedBrands.value.indexOf(name)
  if (i >= 0) selectedBrands.value.splice(i, 1)
  else selectedBrands.value.push(name)
}

// ─── Display helpers ──────────────────────────────────────────────────────
const AVATAR_COLORS = ['#1677ff', '#13c2c2', '#722ed1', '#eb2f96', '#fa8c16']
function avatarColor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) & 0xffff
  return AVATAR_COLORS[h % AVATAR_COLORS.length]
}

function formatPrice(v: number | null): string {
  if (v === null || v === undefined) return '—'
  if (v >= 10000) return `${(v / 10000).toFixed(1)}万`
  return v.toFixed(0)
}

function priceRangeText(r: BrandRecommendation): string {
  if (!r.price_p10 && !r.price_p90) return '暂无价格数据'
  const lo = formatPrice(r.price_p10)
  const hi = formatPrice(r.price_p90)
  const med = formatPrice(r.price_median)
  return `¥${lo} ~ ¥${hi}（中位价 ¥${med}）`
}

const TAG_COLORS: Record<string, string> = {
  '合资品牌':   'purple',
  '国产品牌':   'blue',
  '数据充足':   'green',
  '有参考价格': 'cyan',
}
function tagColor(t: string): string { return TAG_COLORS[t] ?? 'default' }
</script>

<template>
  <div class="invite-page">
    <div class="invite-page__header">
      <div>
        <h1 class="invite-page__title">邀标建议</h1>
        <div class="invite-page__subtitle">
          基于采购品类 · 推荐审定品牌及历史价格参考
        </div>
      </div>
      <a-button disabled>
        <template #icon><HistoryOutlined /></template>
        历史邀标
      </a-button>
    </div>

    <a-row :gutter="16" class="invite-page__body">
      <!-- ════════ 左侧：招标信息 ════════ -->
      <a-col :xs="24" :lg="8" class="invite-page__col">
        <div class="tender-card">
          <div class="tender-card__title">招标信息</div>
          <div class="tender-card__subtitle">上传招标文件，自动识别清单后生成品牌建议</div>

          <div class="tender-card__upload">
            <FileUploadCard
              :type="'tender_bidlist'"
              placeholder="上传招标文件"
              hint="支持 PDF / 扫描件图片；自动识别招标清单及品牌要求"
              @extracted="onExtracted"
            />
          </div>

          <template v-if="sourceJob">
            <!-- 项目名称 -->
            <div class="field">
              <label class="field__label">项目名称</label>
              <a-input
                v-model:value="projectName"
                placeholder="自动识别，可编辑"
                allow-clear
              />
            </div>

            <!-- 品牌要求 -->
            <div class="field">
              <label class="field__label">品牌要求</label>
              <div v-if="brandRequirements.length" class="brand-tags">
                <a-tag v-for="b in brandRequirements" :key="b" color="purple">{{ b }}</a-tag>
              </div>
              <div v-else class="field__placeholder">未识别到品牌要求</div>
            </div>

            <!-- 采购清单 -->
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

        <!-- 左侧按钮 -->
        <div v-if="sourceJob" class="action-bar action-bar--left">
          <a-button
            type="primary"
            block
            :loading="recommending"
            :disabled="!canRecommend"
            @click="generateRecommendations"
          >
            <template #icon><ThunderboltOutlined /></template>
            生成品牌建议
          </a-button>
        </div>
      </a-col>

      <!-- ════════ 右侧：推荐品牌 ════════ -->
      <a-col :xs="24" :lg="16" class="invite-page__col">
        <div class="reco-panel">
          <!-- 信息条 -->
          <div class="reco-header">
            <div class="reco-header__meta">
              <InfoCircleOutlined class="reco-header__icon" />
              <span v-if="recommendations.length">
                已从审定品牌库推荐 <b>{{ recommendations.length }}</b> 个品牌 · 合资优先，同类按样本量排序
              </span>
              <span v-else>
                上传招标文件后，系统将从审定品牌库按品类推荐品牌及参考价格区间
              </span>
            </div>
          </div>

          <!-- 品牌卡片列表 -->
          <div v-if="recommendations.length" class="reco-list">
            <div
              v-for="r in pagedRecs"
              :key="r.brand_name + r.category"
              class="reco-card"
              :class="{ 'reco-card--selected': selectedBrands.includes(r.brand_name) }"
            >
              <!-- 左：徽标 + 品牌名 + 类型 -->
              <div class="reco-card__left">
                <div class="reco-card__head">
                  <div class="reco-card__avatar" :style="{ background: avatarColor(r.brand_name) }">{{ r.brand_name[0] }}</div>
                  <div>
                    <div class="reco-card__name">{{ r.brand_name }}</div>
                    <a-tag :color="r.tier === '合资' ? 'purple' : 'blue'" size="small" class="reco-card__tier">{{ r.tier }}</a-tag>
                  </div>
                </div>
                <div class="reco-card__metrics">
                  <div class="reco-metric">
                    <div class="reco-metric__label">品类</div>
                    <div class="reco-metric__value">{{ r.category }}</div>
                  </div>
                  <div class="reco-metric">
                    <div class="reco-metric__label">历史样本</div>
                    <div class="reco-metric__value" :class="r.sample_count >= 20 ? 'metric-green' : r.sample_count >= 5 ? 'metric-normal' : 'metric-na'">
                      {{ r.sample_count > 0 ? r.sample_count + ' 条' : '—' }}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 中：价格区间 -->
              <div class="reco-card__main">
                <div class="reco-card__reason-title">
                  <ThunderboltOutlined />
                  历史价格参考
                </div>
                <div class="reco-card__price-range" :class="r.sample_count === 0 ? 'price-na' : ''">
                  {{ priceRangeText(r) }}
                </div>
                <div class="reco-card__tags">
                  <a-tag v-for="tag in r.tags" :key="tag" :color="tagColor(tag)" size="small">{{ tag }}</a-tag>
                </div>
              </div>

              <!-- 右：操作按钮 -->
              <div class="reco-card__actions">
                <a-button
                  v-if="!selectedBrands.includes(r.brand_name)"
                  type="primary"
                  size="small"
                  @click="toggleBrand(r.brand_name)"
                >
                  <template #icon><PlusOutlined /></template>
                  加入名单
                </a-button>
                <a-button
                  v-else
                  size="small"
                  class="btn-joined"
                  @click="toggleBrand(r.brand_name)"
                >
                  <template #icon><CheckOutlined /></template>
                  已选
                </a-button>
              </div>
            </div>
          </div>

          <!-- 分页 -->
          <div v-if="recommendations.length > pageSize" class="reco-pagination">
            <a-pagination
              v-model:current="currentPage"
              :total="recommendations.length"
              :page-size="pageSize"
              :show-size-changer="false"
              size="small"
            />
          </div>

          <!-- 空态 -->
          <div v-else-if="!recommendations.length" class="reco-empty">
            <a-empty :description="hasItems ? '点击左侧「生成品牌建议」查看推荐品牌' : '上传招标文件后，这里展示推荐品牌'" />
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

        <!-- 右侧按钮 -->
        <div v-if="recommendations.length" class="action-bar action-bar--right">
          <span class="action-bar__count">
            已选品牌：<b>{{ selectedBrands.length }}</b> / {{ recommendations.length }}
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

// ════════ 右侧面板 ════════
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

// ════════ 品牌卡片 ════════
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

  &--selected {
    border-color: @primary-color;
    box-shadow: 0 0 0 1px @primary-color;
  }

  &__left {
    display: flex;
    flex-direction: column;
    width: 200px;
    flex-shrink: 0;
    padding-right: 16px;
    border-right: 1px solid @border-color-split;
    margin-right: 16px;
  }

  &__head {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    width: 100%;
    margin-bottom: 14px;
  }

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
    line-height: 1.3;
  }

  &__tier {
    margin-top: 4px;
  }

  &__metrics {
    width: 100%;
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }

  &__main { flex: 1; min-width: 0; }

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
    margin-bottom: 6px;

    .anticon { margin-right: 3px; }
  }

  &__price-range {
    font-size: 14px;
    color: @text-color;
    line-height: 1.6;
    margin-bottom: 10px;

    &.price-na {
      color: @text-color-tertiary;
    }
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
.metric-normal { color: @heading-color; }
.metric-na     { color: rgba(0, 0, 0, 0.25); font-weight: 400; }

.btn-joined {
  color: @text-color-secondary;
  border-color: @border-color-base;
  background: @component-background;

  .anticon { color: #52c41a; }
}

.reco-pagination {
  display: flex;
  justify-content: center;
  padding: 12px 0 4px;
}

.reco-saved {
  margin-top: 12px;
}

// ════════ 悬浮按钮卡 ════════
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
