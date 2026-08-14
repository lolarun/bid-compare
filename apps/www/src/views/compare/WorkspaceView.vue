<!--
  design/27 §10 步骤3 —— 供应商主轴工作台骨架（header + materials strip +
  tabs + project bootstrap）。新路由，跟旧 5 步向导（IndexView.vue）并存，
  互不影响；退役旧向导 + 路由重定向是步骤5的范围，这里不动 IndexView.vue。

  三条约束（步骤2复核，2026-08-13）：
  1. 项目回填区分"没抽到"（留空+提示可填）vs"抽到空"（原文没有该字段，静态
     标注，不重复提醒）——见 fieldSourceLabel()。
  2. tab 徽标数字只读 useDoubtInbox，不另起计数。
  3. materials strip 的投标文件上传复用 useSupplierUpload，不重写。
-->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  CloudUploadOutlined, FilePdfOutlined, FileExcelOutlined, LoadingOutlined,
  HistoryOutlined, DownloadOutlined, SolutionOutlined,
} from '@ant-design/icons-vue'
import { projectApi, supplierApi, analysisApi, intakeApi } from '@/api'
import type { Supplier, TenderBidlistResult, BidMatrixResult } from '@/api/client'
import { useSupplierUpload } from '@/composables/useSupplierUpload'
import { useDoubtInbox } from '@/composables/useDoubtInbox'
import QuoteGrid from '@/components/QuoteGrid.vue'
import BidMatrix from './components/BidMatrix.vue'
import type { QuoteGridColumn, DoubtMark } from '@/univer/quoteGridController'

const route = useRoute()
const router = useRouter()

// ─── 项目引导（feedback #1）：新建比价 → 空工作台 → 拖入第一份文档回填 ──────
const projectId = ref<number | null>(route.params.projectId ? Number(route.params.projectId) : null)
const projectName = ref('')
const projectCode = ref('')
const category = ref('')
const projectNameUserEdited = ref(false)   // 用户手动改过名称后，自动回填不再覆盖
const projectCodeUserEdited = ref(false)
const bootstrapping = ref(false)

async function ensureProject(): Promise<number> {
  if (projectId.value) return projectId.value
  bootstrapping.value = true
  try {
    const { data } = await projectApi.create({
      name: projectName.value || '新比价项目', code: projectCode.value, location: '', status: 'active', remark: '',
    })
    projectId.value = data.id
    router.replace(`/workspace/${data.id}`)
    return data.id
  } finally {
    bootstrapping.value = false
  }
}

async function persistProjectMeta() {
  if (!projectId.value) return
  await projectApi.update(projectId.value, { name: projectName.value, code: projectCode.value })
}

async function onProjectNameInput() {
  projectNameUserEdited.value = true
  await persistProjectMeta()
}
async function onProjectCodeInput() {
  projectCodeUserEdited.value = true
  await persistProjectMeta()
}

onMounted(async () => {
  if (projectId.value) {
    try {
      const { data } = await projectApi.get(projectId.value)
      projectName.value = data.name
      projectCode.value = data.code
    } catch { /* 项目不存在时留空，用户可重新新建 */ }
  }
  const { data: suppliers } = await supplierApi.list({ page: 1, page_size: 500 })
  allSuppliers.value = suppliers.items
})

// ─── 供应商列表（useSupplierUpload 需要） ─────────────────────────────────
const allSuppliers = ref<Supplier[]>([])

// ─── Step 2「投标文件」复用 useSupplierUpload（约束3：不重写） ─────────────
const taskConfig = reactive({
  projectId: undefined as number | undefined,
  category: '', supplierIds: [] as number[], bidStatus: '',
})
watch(projectId, (id) => { taskConfig.projectId = id ?? undefined }, { immediate: true })
watch(category, (c) => { taskConfig.category = c }, { immediate: true })

const {
  batchFiles, handleBatchFile, confirmBatchEntry, removeBatchEntry,
} = useSupplierUpload({
  taskConfig,
  tenderCategory: category,
  confirmedCategories: computed(() => (category.value ? [category.value] : [])),
  categoryExplicitlySelected: ref(true),
  allSuppliers,
  onMissingTotalDetails: (fileId) => { activeTab.value = String(fileId) },
})

async function onDropBidFiles(file: File) {
  await ensureProject()
  handleBatchFile(file)
}

// ─── 招标文件 + Excel 清单（materials strip 折叠卡片，非 IndexView 的完整
//     招标识别 UI——完整的三件套分区卡是步骤4 的范围，这里只要"识别完成/
//     未识别"两态可用） ──────────────────────────────────────────────────
const tenderFile = ref<File | null>(null)
const tenderResult = ref<TenderBidlistResult | null>(null)
const tenderUploading = ref(false)
const tenderError = ref('')
let tenderPollTimer: ReturnType<typeof setInterval> | null = null

async function uploadTender(file: File) {
  const pid = await ensureProject()
  tenderFile.value = file
  tenderUploading.value = true
  tenderError.value = ''
  const form = new FormData()
  form.append('file', file)
  form.append('type', 'tender_bidlist')
  form.append('project_id', String(pid))
  try {
    const { data } = await intakeApi.upload(form)
    if (data.status === 'done') onTenderDone(data.result as unknown as TenderBidlistResult)
    else if (data.status === 'failed') { tenderUploading.value = false; tenderError.value = data.error || '识别失败' }
    else pollTender(data.id)
  } catch (e: unknown) {
    tenderUploading.value = false
    tenderError.value = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '上传失败'
  }
}

function pollTender(jobId: string) {
  if (tenderPollTimer) clearInterval(tenderPollTimer)
  let failures = 0
  tenderPollTimer = setInterval(async () => {
    try {
      const { data } = await intakeApi.getJob(jobId)
      failures = 0
      if (data.status === 'done') {
        if (tenderPollTimer) clearInterval(tenderPollTimer)
        onTenderDone(data.result as unknown as TenderBidlistResult)
      } else if (data.status === 'failed') {
        if (tenderPollTimer) clearInterval(tenderPollTimer)
        tenderUploading.value = false
        tenderError.value = data.error || '识别失败'
      }
    } catch {
      failures++
      if (failures >= 5) {
        if (tenderPollTimer) clearInterval(tenderPollTimer)
        tenderUploading.value = false
        tenderError.value = '轮询超时，请重试'
      }
    }
  }, 2000)
}

function onTenderDone(result: TenderBidlistResult) {
  tenderUploading.value = false
  tenderResult.value = result
  if (result.detected_category && !category.value) category.value = result.detected_category
  // 项目回填（约束1）：只在用户没手动改过时覆盖，覆盖的是"识别到的值"，
  // 抽不到时 project_name/project_code 是空字符串，不会覆盖成空——用户已
  // 输入的内容不会被清空。
  if (result.project_name && !projectNameUserEdited.value) projectName.value = result.project_name
  if (result.project_code && !projectCodeUserEdited.value) projectCode.value = result.project_code
  if (projectId.value) persistProjectMeta()
}

const excelFile = ref<File | null>(null)
const excelPreviewing = ref(false)
async function uploadExcel(file: File) {
  await ensureProject()
  excelFile.value = file
  excelPreviewing.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    const { data } = await analysisApi.tenderListPreview(form)
    if (data.detected_category && !category.value) category.value = data.detected_category
    message.success(`采购清单已预览：${data.total} 条`)
  } catch (e: unknown) {
    message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '预览失败')
  } finally {
    excelPreviewing.value = false
  }
}

// ─── 疑点收件箱（约束2：tab 徽标只读这里，不另起计数） ─────────────────────
const { dryRunByFile, refreshDryRun } = useDoubtInbox({
  batchFiles,
  taskConfig,
  reconcileResult: ref(null),
  reconcileConfirmed: ref(false),
  anchorReviewResult: ref(null),
  onGoToFile: (fileId: string) => { activeTab.value = fileId },
  onGoToReconcile: () => {},
  onGoToAlignment: () => { if (projectId.value) router.push(`/compare/${projectId.value}/align`) },
})

function badgeCount(fileId: string): number {
  return dryRunByFile[fileId]?.issues?.length ?? 0
}

const dryRunAutoChecked = new Set<string>()
watch(batchFiles, (files) => {
  for (const f of files) {
    if (f.status === 'done' && !f.confirmed && f.finalSupplierName.trim() && !dryRunAutoChecked.has(f.id)) {
      dryRunAutoChecked.add(f.id)
      refreshDryRun(f)
    }
  }
}, { deep: true })

// ─── Tabs ──────────────────────────────────────────────────────────────
const activeTab = ref('list')
watch(batchFiles, (files) => {
  // 首次有报价文件时自动切到第一个 tab，体验上少一次点击；之后不再自动跳
  // （用户可能正在看别的 tab）。
  if (activeTab.value === 'list' && files.length === 1) activeTab.value = files[0].id
}, { deep: true })

const gridColumns: QuoteGridColumn[] = [
  { key: 'material', title: '材料/设备名称' },
  { key: 'spec', title: '规格型号' },
  { key: 'unit', title: '计量单位' },
  { key: 'qty', title: '数量' },
  { key: 'unit_price', title: '单价' },
  { key: 'total_price', title: '合价' },
  { key: 'brand', title: '品牌' },
  { key: 'remark', title: '备注' },
]

function doubtMarksFor(fileId: string): DoubtMark[] {
  const dr = dryRunByFile[fileId]
  if (!dr?.issues?.length) return []
  // dry-run 的四道门是整份文件级别的问题（结构完整性/无合价等），暂时没有
  // 逐行逐列坐标——先落一条整体提示在第一行首列，逐格定位是步骤4 的范围
  // （那边有 doubtCopy.ts 的完整翻译表可用）。
  return [{ row: 0, columnKey: 'material', severity: 'missing', hoverText: dr.issues.map((i) => i.message).join('\n') }]
}

// ─── Step 4「结果」：复用 BidMatrix.vue ────────────────────────────────────
const matrixResult = ref<BidMatrixResult | null>(null)
const analyzing = ref(false)
const confirmedSubmissionIds = computed(() =>
  batchFiles.value.filter((f) => f.confirmed && f.confirmedSubmissionId != null).map((f) => f.confirmedSubmissionId!))

async function runAnalysis() {
  if (!projectId.value || confirmedSubmissionIds.value.length === 0) {
    message.warning('请至少完成一家供应商的「校对入库」再开始比价')
    return
  }
  analyzing.value = true
  try {
    const { data } = await analysisApi.bidMatrix({
      project_id: projectId.value, supplier_ids: [], submission_ids: confirmedSubmissionIds.value, category: category.value,
    })
    matrixResult.value = data
  } catch (e: unknown) {
    message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '比价分析失败')
  } finally {
    analyzing.value = false
  }
}

const matrixSuppliers = computed(() => matrixResult.value?.suppliers ?? [])
</script>

<template>
  <div class="workspace-view">
    <!-- Header：项目名/编号（回填+可编辑）+ 品类 + 供应商数 + 操作按钮 -->
    <div class="workspace-header">
      <div class="workspace-header__title">
        <a-input
          v-model:value="projectName"
          placeholder="比价项目名称（拖入招标文件后自动识别，可编辑）"
          :bordered="false"
          style="font-size:18px;font-weight:600;padding:0"
          @change="onProjectNameInput"
        />
        <div class="workspace-header__meta">
          <a-input
            v-model:value="projectCode"
            placeholder="编号（未识别，可填写）"
            :bordered="false"
            size="small"
            style="width:160px;color:rgba(0,0,0,0.55)"
            @change="onProjectCodeInput"
          />
          <span v-if="category">· {{ category }}</span>
          <span v-if="batchFiles.length">· {{ batchFiles.length }} 家投标</span>
        </div>
      </div>
      <div class="workspace-header__actions">
        <a-button @click="() => { if (projectId) router.push(`/compare/${projectId}/align`) }">
          <SolutionOutlined />对齐核查
        </a-button>
        <a-button><HistoryOutlined />历史</a-button>
        <a-button type="primary"><DownloadOutlined />导出</a-button>
      </div>
    </div>

    <!-- Materials strip -->
    <div class="materials-strip">
      <div class="materials-strip__card">
        <FilePdfOutlined style="color:#cf1322" />
        <span v-if="tenderResult">招标文件 ✓ {{ tenderResult.row_count }} 项</span>
        <span v-else-if="tenderUploading"><LoadingOutlined spin /> 识别中…</span>
        <a-upload v-else :show-upload-list="false" accept=".pdf,.png,.jpg,.jpeg"
          :before-upload="(f: File) => { uploadTender(f); return false }">
          <a-button size="small" type="dashed">上传招标文件</a-button>
        </a-upload>
        <a-alert v-if="tenderError" type="error" :message="tenderError" show-icon banner style="padding:2px 8px" />
      </div>

      <div class="materials-strip__card">
        <FileExcelOutlined style="color:#52c41a" />
        <span v-if="excelFile">采购清单 Excel ✓</span>
        <span v-else-if="excelPreviewing"><LoadingOutlined spin /> 解析中…</span>
        <a-upload v-else :show-upload-list="false" accept=".xlsx,.xls"
          :before-upload="(f: File) => { uploadExcel(f); return false }">
          <a-button size="small" type="dashed">上传采购清单 Excel</a-button>
        </a-upload>
      </div>

      <a-upload :show-upload-list="false" :multiple="true" accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls,.csv"
        :before-upload="(f: File) => { onDropBidFiles(f); return false }">
        <a-button type="dashed"><CloudUploadOutlined /> 拖入投标文件</a-button>
      </a-upload>
    </div>

    <!-- Tabs -->
    <a-tabs v-model:active-key="activeTab" class="workspace-tabs">
      <a-tab-pane key="list">
        <template #tab>清单{{ tenderResult ? ` · ${tenderResult.row_count}` : '' }}</template>
        <div style="padding:12px;color:rgba(0,0,0,0.45);font-size:13px">
          采购清单只读（对齐核查页处理逐行裁决，清单本身如有误请「重新上传」）——
          步骤4 接入完整清单表格视图。
        </div>
      </a-tab-pane>
      <a-tab-pane v-for="f in batchFiles" :key="f.id">
        <template #tab>
          {{ f.finalSupplierName || f.filename }}
          <a-badge v-if="badgeCount(f.id) > 0" :count="badgeCount(f.id)" :offset="[4, -2]" />
        </template>
        <div class="supplier-tab-content">
          <div v-if="f.status !== 'done' || f.confirmed === false" class="supplier-tab-content__progress">
            <template v-if="f.status === 'uploading' || f.status === 'processing'">
              <LoadingOutlined spin /> {{ f.stage }}<span v-if="f.stageDetail">（{{ f.stageDetail }}）</span> · {{ f.progressPct }}%
            </template>
            <template v-else-if="f.status === 'failed'">
              <span style="color:#ff4d4f">{{ f.error || '识别失败' }}</span>
            </template>
          </div>
          <template v-if="f.status === 'done'">
            <div class="supplier-tab-content__meta">
              <a-auto-complete
                v-model:value="f.finalSupplierName"
                placeholder="供应商名称"
                :options="allSuppliers.map(s => ({ value: s.name, label: s.name, id: s.id }))"
                @select="(_v: string, opt: { id?: number }) => { f.matchedSupplierId = opt.id ?? null }"
                style="width:220px"
              />
              <a-button type="primary" :loading="f.confirming" @click="confirmBatchEntry(f)">确认入库</a-button>
              <a-button danger @click="removeBatchEntry(f)">移除</a-button>
            </div>
            <QuoteGrid
              :model-value="f.items as unknown as Record<string, unknown>[]"
              :columns="gridColumns"
              :doubt-marks="doubtMarksFor(f.id)"
              @update:model-value="(v) => { f.items = v as any }"
            />
          </template>
        </div>
      </a-tab-pane>
    </a-tabs>

    <!-- Result section -->
    <div class="result-section">
      <a-button type="primary" :loading="analyzing" @click="runAnalysis">
        {{ matrixResult ? '重新分析' : '开始比价分析' }}
      </a-button>
      <BidMatrix
        v-if="matrixResult"
        :suppliers="matrixSuppliers"
        :rows="matrixResult.rows"
        :totals="matrixResult.totals"
        :category="category"
        :project-id="projectId ?? undefined"
        style="margin-top:16px"
      />
    </div>
  </div>
</template>

<style scoped>
.workspace-view { padding: 16px 24px; max-width: 1400px; margin: 0 auto; }
.workspace-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.workspace-header__title { flex: 1; min-width: 0; }
.workspace-header__meta { display: flex; align-items: center; gap: 6px; font-size: 13px; color: rgba(0,0,0,0.45); margin-top: 4px; }
.workspace-header__actions { display: flex; gap: 8px; flex-shrink: 0; }
.materials-strip { display: flex; align-items: center; gap: 16px; padding: 12px 16px; background: #fafafa; border-radius: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.materials-strip__card { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.workspace-tabs { background: #fff; }
.supplier-tab-content { padding: 8px 0; }
.supplier-tab-content__progress { padding: 24px; text-align: center; color: rgba(0,0,0,0.55); }
.supplier-tab-content__meta { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.result-section { margin-top: 24px; padding-top: 16px; border-top: 1px solid #f0f0f0; }
</style>
