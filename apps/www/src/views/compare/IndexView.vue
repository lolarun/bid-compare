<script setup lang="ts">
import { ref, computed, reactive, onMounted, watch } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleOutlined, LineChartOutlined } from '@ant-design/icons-vue'
import { projectApi, supplierApi, analysisApi, quoteApi } from '@/api'
import type {
  Project,
  Supplier,
  BidMatrixResult,
  ExtractionJob,
  QuoteExtractionItem,
  BatchConfirmResult,
} from '@/api/client'
import IntakeUploader from '@/components/IntakeUploader.vue'
import ExtractionEditor from '@/components/ExtractionEditor.vue'
import BrandTierModal from '@/components/BrandTierModal.vue'
import BidMatrix from './components/BidMatrix.vue'

const CATEGORIES = [
  '桥架', '母线槽', '配电箱',
  '阀门', '不锈钢管', '水箱', '潜水泵',
  '风口风阀', '风机盘管', '空调泵',
] as const

// ─── State ───────────────────────────────────────────────────────────────
const currentStep = ref(0)

const taskConfig = reactive<{
  projectId: number | undefined
  category: string
  supplierIds: number[]
}>({
  projectId: undefined,
  category: '',
  supplierIds: [],
})

const projects = ref<Project[]>([])
const allSuppliers = ref<Supplier[]>([])

// Per-supplier upload state for Step 2
const supplierUploads = reactive<Record<number, {
  job: ExtractionJob | null
  items: QuoteExtractionItem[]
  confirmed: boolean
  batch_id?: string
  unknown_brands: string[]
}>>({})

// Bid matrix result for Step 3
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)

// Brand-tier modal
const brandModalVisible = ref(false)
const brandsToTier = ref<string[]>([])

// ─── Computed ────────────────────────────────────────────────────────────
const canProceedFromConfig = computed(
  () => !!taskConfig.category && taskConfig.supplierIds.length >= 2
)
const canProceedFromUpload = computed(
  () => taskConfig.supplierIds.every(
    (sid) => supplierUploads[sid]?.confirmed || (supplierUploads[sid]?.items?.length ?? 0) > 0
  )
)

const selectedSuppliers = computed(() =>
  allSuppliers.value.filter((s) => taskConfig.supplierIds.includes(s.id))
)

const matrixRows = computed(() => matrixResult.value?.rows ?? [])
const matrixTotals = computed(() => matrixResult.value?.totals ?? [])
const matrixSuppliers = computed(() => matrixResult.value?.suppliers ?? [])
const matrixSummary = computed(() => {
  if (!matrixResult.value) return null
  const rows = matrixResult.value.rows
  const totals = matrixResult.value.totals
  const suppliers = matrixResult.value.suppliers
  const best = totals.length
    ? totals.reduce((a, b) => (a.avg_deviation < b.avg_deviation ? a : b))
    : null
  const bestSupplier = best ? suppliers.find((s) => s.id === best.supplier_id) : null
  // Optimal total: sum of min prices per row
  const optimalTotal = rows.reduce((sum, row) => {
    const tots = row.suppliers.filter((c) => c.total !== null).map((c) => c.total as number)
    return sum + (tots.length ? Math.min(...tots) : 0)
  }, 0)
  const anomalyCount = rows.reduce(
    (n, r) => n + r.suppliers.filter((c) => c.alert_level === 'red').length, 0,
  )
  return {
    total_materials: rows.length,
    total_suppliers: suppliers.length,
    recommended_supplier: bestSupplier,
    optimal_total: Math.round(optimalTotal),
    anomaly_count: anomalyCount,
  }
})

// ─── Data fetching ───────────────────────────────────────────────────────
async function fetchProjects() {
  try {
    const { data } = await projectApi.list({ page: 1, page_size: 200 })
    projects.value = data.items
  } catch {
    projects.value = []
  }
}
async function fetchSuppliers() {
  try {
    const { data } = await supplierApi.list({ page: 1, page_size: 300 })
    allSuppliers.value = data.items
  } catch {
    allSuppliers.value = []
  }
}

onMounted(() => {
  fetchProjects()
  fetchSuppliers()
})

// Initialize a slot whenever a new supplier is selected
watch(() => taskConfig.supplierIds, (ids) => {
  for (const sid of ids) {
    if (!supplierUploads[sid]) {
      supplierUploads[sid] = {
        job: null, items: [], confirmed: false, unknown_brands: [],
      }
    }
  }
}, { immediate: true })

// ─── Step navigation ─────────────────────────────────────────────────────
function goNext() {
  if (currentStep.value === 0) {
    if (!canProceedFromConfig.value) {
      message.warning('请选择品类并至少 2 家供应商')
      return
    }
    currentStep.value = 1
  } else if (currentStep.value === 1) {
    if (!canProceedFromUpload.value) {
      message.warning('请为每家供应商上传报价单或确认其已有数据')
      return
    }
    currentStep.value = 2
    runMatrix()
  }
}

function goBack() {
  if (currentStep.value > 0) currentStep.value -= 1
}

// ─── Step 2: per-supplier upload handlers ────────────────────────────────
function onExtracted(supplierId: number, job: ExtractionJob) {
  const items = ((job.result as { items?: QuoteExtractionItem[] })?.items ?? []) as QuoteExtractionItem[]
  supplierUploads[supplierId] = {
    ...(supplierUploads[supplierId] || { items: [], confirmed: false, unknown_brands: [] }),
    job,
    items,
    confirmed: false,
  }
}

async function confirmSupplier(supplierId: number) {
  const slot = supplierUploads[supplierId]
  if (!slot || !slot.job) {
    message.warning('请先上传该供应商的报价单')
    return
  }
  try {
    const { data } = await quoteApi.batchConfirm({
      job_id: slot.job.id,
      supplier_id: supplierId,
      project_id: taskConfig.projectId,
      category: taskConfig.category,
      overrides: slot.items as unknown as Array<Record<string, unknown>>,
    })
    const result = data as BatchConfirmResult
    slot.confirmed = true
    slot.batch_id = result.batch_id
    slot.unknown_brands = result.unknown_brands || []
    message.success(`已入库 ${result.created} 条报价`)
    if (slot.unknown_brands.length > 0) {
      brandsToTier.value = slot.unknown_brands
      brandModalVisible.value = true
    }
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '入库失败'
    message.error(detail)
  }
}

// Skip upload for a supplier (use existing historical data)
function skipSupplier(supplierId: number) {
  supplierUploads[supplierId] = {
    job: null,
    items: [],
    confirmed: true,
    unknown_brands: [],
  }
  message.info('已跳过该供应商上传，将使用历史数据')
}

// ─── Step 3: run bid-matrix ──────────────────────────────────────────────
async function runMatrix() {
  if (taskConfig.supplierIds.length < 2) return
  analyzing.value = true
  matrixResult.value = null
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: taskConfig.projectId,
      supplier_ids: taskConfig.supplierIds,
    })
    matrixResult.value = data
    if ((data.rows ?? []).length === 0) {
      message.warning('当前条件下未找到可比的报价数据')
    }
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? '比价分析失败'
    message.error(detail)
  } finally {
    analyzing.value = false
  }
}
</script>

<template>
  <div class="compare-page">
    <!-- Page header -->
    <div class="compare-page__header">
      <div>
        <h1 class="compare-page__title">招标比价分析</h1>
        <div class="compare-page__subtitle">
          按"配置→录入报价→比价结果"分步完成；支持 PDF/扫描件自动识别
        </div>
      </div>
    </div>

    <!-- Steps indicator -->
    <a-steps :current="currentStep" style="margin-bottom:20px">
      <a-step title="配置任务" description="项目 + 品类 + 供应商" />
      <a-step title="录入报价" description="按供应商上传或使用历史数据" />
      <a-step title="比价结果" description="横向矩阵 + 推荐供应商" />
    </a-steps>

    <!-- Step 0: Configure -->
    <a-card v-if="currentStep === 0" :body-style="{ padding: '20px' }">
      <a-form layout="vertical">
        <a-form-item label="项目（可选；不选则跨项目比价）">
          <a-select
            v-model:value="taskConfig.projectId"
            placeholder="选择项目"
            allow-clear
            show-search
            :filter-option="(input: string, opt: { label?: unknown }) => String(opt.label ?? '').includes(input)"
          >
            <a-select-option
              v-for="p in projects"
              :key="p.id"
              :value="p.id"
              :label="p.name"
            >
              {{ p.name }}
              <span v-if="p.code" style="color:rgba(0,0,0,0.45);margin-left:6px">{{ p.code }}</span>
            </a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="品类（必选）" required>
          <a-select v-model:value="taskConfig.category" placeholder="选择品类" style="width:280px">
            <a-select-option v-for="c in CATEGORIES" :key="c" :value="c">{{ c }}</a-select-option>
          </a-select>
        </a-form-item>

        <a-form-item label="参与供应商（≥2 家）" required>
          <a-select
            v-model:value="taskConfig.supplierIds"
            mode="multiple"
            placeholder="选择 2-N 家参与比价的供应商"
            show-search
            :filter-option="(input: string, opt: { label?: unknown }) => String(opt.label ?? '').includes(input)"
            style="width:100%"
          >
            <a-select-option
              v-for="s in allSuppliers"
              :key="s.id"
              :value="s.id"
              :label="s.name"
            >
              {{ s.name }}
            </a-select-option>
          </a-select>
          <div style="margin-top:6px;font-size:12px;color:rgba(0,0,0,0.45)">
            已选 {{ taskConfig.supplierIds.length }} 家
          </div>
        </a-form-item>
      </a-form>
    </a-card>

    <!-- Step 1: Upload per supplier -->
    <a-card v-else-if="currentStep === 1" :body-style="{ padding: '20px' }">
      <a-tabs :tab-position="'left'">
        <a-tab-pane
          v-for="s in selectedSuppliers"
          :key="s.id"
          :tab="`${s.name}${supplierUploads[s.id]?.confirmed ? ' ✓' : ''}`"
        >
          <div class="upload-pane">
            <div class="upload-pane__title">
              {{ s.name }} 报价单上传
              <a-tag v-if="supplierUploads[s.id]?.confirmed" color="green">已确认</a-tag>
            </div>

            <IntakeUploader
              v-if="!supplierUploads[s.id]?.confirmed"
              :type="'quote'"
              :context="{ supplier_id: s.id, project_id: taskConfig.projectId, category: taskConfig.category }"
              @extracted="(job) => onExtracted(s.id, job)"
            />

            <div v-if="(supplierUploads[s.id]?.items?.length ?? 0) > 0" style="margin-top:14px">
              <a-alert
                type="info"
                show-icon
                message="识别完成，请核对后点击「确认入库」"
                style="margin-bottom:10px"
              />
              <ExtractionEditor
                schema="quote"
                :model-value="supplierUploads[s.id]?.items as unknown[] as any"
                :confirm-label="'确认入库'"
                @confirm="() => confirmSupplier(s.id)"
                @update:model-value="(v: any) => supplierUploads[s.id].items = v"
              />
            </div>

            <div v-else style="margin-top:14px;text-align:center">
              <a-button @click="skipSupplier(s.id)">
                使用历史数据，跳过上传
              </a-button>
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- Step 2: Results -->
    <a-card v-else-if="currentStep === 2" :body-style="{ padding: '20px' }">
      <template #title>
        <span style="font-size:15px;font-weight:600">横向对比矩阵</span>
      </template>
      <template #extra>
        <a-space v-if="matrixSummary">
          <a-tag color="blue">{{ matrixSummary.total_materials }} 物料</a-tag>
          <a-tag color="purple">{{ matrixSummary.total_suppliers }} 供应商</a-tag>
          <a-tag v-if="matrixSummary.recommended_supplier" color="green">
            推荐 {{ matrixSummary.recommended_supplier.name }}
          </a-tag>
          <a-tag color="orange">最优总价 ¥{{ matrixSummary.optimal_total.toLocaleString() }}</a-tag>
          <a-tag color="red">异常 {{ matrixSummary.anomaly_count }} 项</a-tag>
        </a-space>
      </template>

      <a-empty v-if="!analyzing && matrixRows.length === 0" description="当前条件下无可比数据" />
      <BidMatrix
        v-else
        :suppliers="matrixSuppliers"
        :rows="matrixRows"
        :totals="matrixTotals"
        :loading="analyzing"
      />
    </a-card>

    <!-- Footer nav -->
    <div class="compare-page__footer">
      <a-button v-if="currentStep > 0" @click="goBack">上一步</a-button>
      <a-button v-if="currentStep < 2" type="primary" @click="goNext">
        下一步
        <template #icon><CheckCircleOutlined /></template>
      </a-button>
      <a-button v-if="currentStep === 2" type="primary" @click="runMatrix">
        <template #icon><LineChartOutlined /></template>
        重新比价
      </a-button>
    </div>

    <BrandTierModal
      v-model:visible="brandModalVisible"
      :brands="brandsToTier"
      :category="taskConfig.category"
    />
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.compare-page {
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
  &__footer {
    margin-top: 20px;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
}

.upload-pane {
  &__title {
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 12px;
    color: @heading-color;
  }
}
</style>
