<script setup lang="ts">
import { ref, watch, onBeforeUnmount, onMounted } from 'vue'
import { message, Upload, type UploadProps } from 'ant-design-vue'
import {
  InboxOutlined,
  FileExcelOutlined,
  ScanOutlined,
  DownloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { quoteApi, intakeApi, projectApi } from '@/api'
import type { EnhanceSummary } from '@/api/client'
import ExtractionEditor from '@/components/ExtractionEditor.vue'

const activeTab = ref<'excel' | 'ocr'>('excel')

// ─── Excel 导入 ──────────────────────────────────────────────────────────
const excelFileList = ref<UploadProps['fileList']>([])
const excelImporting = ref(false)
const excelResult = ref<{ batch_id: string; imported: number; skipped: number; errors: Record<string, unknown>[] } | null>(null)

const excelTemplates = [
  { name: '桥架', cols: '名称、规格、材质、厚度×3、单价、品牌' },
  { name: '阀门', cols: '名称、规格、型号、材质×5、价税合计、品牌' },
  { name: '风口风阀', cols: '名称、型号、规格、钢板厚度、含税单价、品牌' },
  { name: '母线槽', cols: '名称、母线类型、规格型号、铜牌厚度、含税单价、品牌' },
  { name: '配电箱', cols: '元器件名称、品牌、系列、规格、数量、单价（按元器件拆分）' },
  { name: '不锈钢管', cols: '名称、规格、壁厚、牌号、含税单价、品牌' },
  { name: '水箱', cols: '名称、规格型号、价税合计、品牌' },
  { name: '潜水泵', cols: '名称、型号、流量/扬程/功率、单价、品牌' },
  { name: '风机盘管', cols: '名称、型号、管制、风量、单价合计、品牌' },
  { name: '空调泵', cols: '名称、规格、流量/扬程/功率、单价、品牌' },
]

const selectedTemplate = ref<string>('桥架')

const TEMPLATE_HEADERS: Record<string, string[]> = {
  '桥架': ['名称', '规格', '材质', '厚度', '单价', '品牌', '供应商'],
  '阀门': ['名称', '规格', '型号', '材质', '价税合计', '品牌', '供应商'],
  '风口风阀': ['名称', '型号', '规格', '钢板厚度', '含税单价', '品牌', '供应商'],
  '母线槽': ['名称', '母线类型', '规格型号', '铜牌厚度', '含税单价', '品牌', '供应商'],
  '配电箱': ['元器件名称', '品牌', '系列', '规格', '数量', '单价', '供应商'],
  '不锈钢管': ['名称', '规格', '壁厚', '牌号', '含税单价', '品牌', '供应商'],
  '水箱': ['名称', '规格型号', '价税合计', '品牌', '供应商'],
  '潜水泵': ['名称', '型号', '流量', '扬程', '功率', '单价', '品牌', '供应商'],
  '风机盘管': ['名称', '型号', '管制', '风量', '单价合计', '品牌', '供应商'],
  '空调泵': ['名称', '规格', '流量', '扬程', '功率', '单价', '品牌', '供应商'],
}

function downloadTemplate() {
  const headers = TEMPLATE_HEADERS[selectedTemplate.value]
  if (!headers) {
    message.warning('未找到该品类的模板')
    return
  }
  const bom = '﻿'
  const csv = bom + headers.join(',') + '\n'
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${selectedTemplate.value}_导入模板.csv`
  a.click()
  URL.revokeObjectURL(url)
}

async function doExcelImport() {
  if (!excelFileList.value || excelFileList.value.length === 0) {
    message.warning('请选择 Excel 文件')
    return
  }
  excelImporting.value = true
  const form = new FormData()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const file = (excelFileList.value[0] as any).originFileObj as File
  form.append('file', file)
  form.append('category', selectedTemplate.value)
  try {
    const { data } = await quoteApi.import(form)
    excelResult.value = data
    message.success(`成功导入 ${data.imported} 条`)
  } catch (e) {
    message.error('Excel 导入失败，请检查模板格式')
  } finally {
    excelImporting.value = false
  }
}

const excelDraggerProps: UploadProps = {
  name: 'file',
  multiple: false,
  beforeUpload: (file: File) => {
    excelFileList.value = [
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      { uid: String(Date.now()), name: file.name, status: 'done', originFileObj: file } as any,
    ]
    return false
  },
  onRemove: () => { excelFileList.value = [] },
}

// ─── OCR 扫描件 ──────────────────────────────────────────────────────────
const ocrFile = ref<File | null>(null)
const ocrPreviewUrl = ref<string | null>(null)
const ocrParsing = ref(false)
const ocrEnhancing = ref(false)
const ocrJobId = ref<string | null>(null)
const ocrSupplierName = ref<string>('')
const ocrProjectId = ref<number | null>(null)
const enhanceSummary = ref<EnhanceSummary | null>(null)
const projectList = ref<Array<{ id: number; name: string }>>([])

// Load project list for dropdown
onMounted(async () => {
  try {
    const { data } = await projectApi.list({ page_size: 200 })
    projectList.value = data.items.map((p) => ({ id: p.id, name: p.name }))
  } catch {
    // ignore
  }
})

const ocrResult = ref<Array<{
  material: string; spec: string; brand: string; unit: string;
  qty: number | null; unit_price: number | null;
  unit_price_excl_tax: number | null; total_price: number | null;
  tax_rate: number | null; remark: string;
  // AI-enhanced fields
  category?: string; original_name?: string; name_note?: string;
  alignment_note?: string; matched_material_id?: number | null;
}> | null>(null)

const ocrDraggerProps: UploadProps = {
  name: 'file',
  multiple: false,
  accept: '.pdf,.png,.jpg,.jpeg',
  beforeUpload: (file: File) => {
    ocrFile.value = file
    ocrPreviewUrl.value = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
    parseOcr()
    return false
  },
}

watch(ocrPreviewUrl, (_, old) => { if (old) URL.revokeObjectURL(old) })
onBeforeUnmount(() => { if (ocrPreviewUrl.value) URL.revokeObjectURL(ocrPreviewUrl.value) })

/** Extract items from a completed job result and call AI enhance. */
async function applyEnhance(jobId: string, rawItems: Array<Record<string, unknown>>, supplierFromOcr: string) {
  // Auto-populate supplier name from OCR if not already set
  if (!ocrSupplierName.value && supplierFromOcr) {
    ocrSupplierName.value = supplierFromOcr
  }

  ocrEnhancing.value = true
  try {
    const { data: enhanced } = await intakeApi.enhance({
      job_id: jobId,
      project_id: ocrProjectId.value,
    })

    enhanceSummary.value = enhanced.summary

    // Map enhanced items → ocrResult rows
    // Use standard_name as material (the AI-corrected name), keep AI metadata for highlights
    ocrResult.value = enhanced.items.map((it) => ({
      material: it.standard_name || it.material,
      spec: it.standard_spec || it.spec,
      brand: it.brand,
      unit: it.unit,
      qty: it.qty,
      unit_price: it.unit_price,
      unit_price_excl_tax: it.unit_price_excl_tax,
      total_price: it.total_price,
      tax_rate: it.tax_rate,
      remark: it.remark,
      category: it.category,
      original_name: it.original_name,
      name_note: it.name_note,
      alignment_note: it.alignment_note,
      matched_material_id: it.matched_material_id,
    }))

    const s = enhanced.summary
    const parts: string[] = [`共 ${s.total} 行`]
    if (s.categorized > 0) parts.push(`自动分类 ${s.categorized} 项`)
    if (s.renamed > 0) parts.push(`名称标准化 ${s.renamed} 项`)
    if (s.aligned > 0) parts.push(`可对齐 ${s.aligned} 项`)
    message.success(`AI 增强完成：${parts.join('，')}`)
  } catch (e) {
    // Enhance failed — fall back to raw OCR items without highlights
    message.warning('AI 增强失败，使用原始 OCR 结果')
    ocrResult.value = rawItems.map((it) => ({
      material: String(it.material || ''),
      spec: String(it.spec || ''),
      brand: String(it.brand || ''),
      unit: String(it.unit || ''),
      qty: it.qty != null ? Number(it.qty) : null,
      unit_price: it.unit_price != null ? Number(it.unit_price) : null,
      unit_price_excl_tax: it.unit_price_excl_tax != null ? Number(it.unit_price_excl_tax) : null,
      total_price: it.total_price != null ? Number(it.total_price) : null,
      tax_rate: it.tax_rate != null ? Number(it.tax_rate) : null,
      remark: String(it.remark || ''),
    }))
  } finally {
    ocrEnhancing.value = false
  }

}

async function parseOcr() {
  if (!ocrFile.value) return
  ocrParsing.value = true
  ocrResult.value = null
  enhanceSummary.value = null
  ocrJobId.value = null
  try {
    const form = new FormData()
    form.append('file', ocrFile.value)
    form.append('type', 'quote')
    const { data: job } = await intakeApi.upload(form)
    const jobId = job.id
    let status = job.status

    // Poll until done
    while (status === 'pending' || status === 'running') {
      await new Promise((r) => setTimeout(r, 2000))
      const { data: poll } = await intakeApi.getJob(jobId)
      status = poll.status
      if (status === 'done' && poll.result) {
        const items = (poll.result as Record<string, unknown>).items as Array<Record<string, unknown>> | undefined
        const supplierFromOcr = String((poll.result as Record<string, unknown>).supplier_name || '')
        ocrParsing.value = false
        if (items && items.length > 0) {
          ocrJobId.value = jobId
          message.info(`OCR 识别 ${items.length} 行，正在 AI 增强…`)
          await applyEnhance(jobId, items, supplierFromOcr)
        } else {
          message.warning('OCR 未识别到报价行，请检查文件内容')
        }
        return
      }
      if (status === 'failed') {
        message.error(`OCR 解析失败：${poll.error || '未知错误'}`)
        return
      }
    }
    // Synchronous done
    if (status === 'done' && job.result) {
      const items = (job.result as Record<string, unknown>).items as Array<Record<string, unknown>> | undefined
      const supplierFromOcr = String((job.result as Record<string, unknown>).supplier_name || '')
      ocrParsing.value = false
      if (items && items.length > 0) {
        ocrJobId.value = jobId
        message.info(`OCR 识别 ${items.length} 行，正在 AI 增强…`)
        await applyEnhance(jobId, items, supplierFromOcr)
      }
    }
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'OCR 解析失败'
    message.error(detail)
  } finally {
    ocrParsing.value = false
    ocrEnhancing.value = false
  }
}

async function onOcrConfirm(rows: unknown[]) {
  if (!ocrJobId.value) {
    message.error('缺少任务 ID，请重新上传文件')
    return
  }
  const supplierName = ocrSupplierName.value.trim()
  if (!supplierName) {
    message.error('请填写供应商名称后再入库')
    return
  }
  try {
    const { data } = await quoteApi.batchConfirm({
      job_id: ocrJobId.value,
      supplier_name: supplierName,
      project_id: ocrProjectId.value ?? undefined,
      category: '',  // per-item category comes from enhanced items
      overrides: rows as Array<Record<string, unknown>>,
    })
    if (data.line_count > 0) {
      message.success(`入库成功：新增 ${data.line_count} 条，跳过 ${data.skipped_count} 条`)
    } else {
      message.warning(`未新增任何报价（跳过 ${data.skipped_count} 条，错误 ${data.errors?.length ?? 0} 条）`)
    }
    if (data.errors && data.errors.length > 0) {
      const errMsg = data.errors.slice(0, 3).map((e) => `第${e.row}行: ${e.reason}`).join('；')
      message.warning(`部分行跳过：${errMsg}`)
    }
    // Reset OCR state
    ocrResult.value = null
    ocrFile.value = null
    ocrPreviewUrl.value = null
    ocrJobId.value = null
    enhanceSummary.value = null
    ocrSupplierName.value = ''
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '入库失败'
    message.error(detail)
  }
}

</script>

<template>
  <div class="import-page">
    <!-- 标题 -->
    <div class="import-page__header">
      <div>
        <h1 class="import-page__title">采购价格导入</h1>
        <div class="import-page__subtitle">
          Excel 批量导入 · PDF/JPG 自动 OCR 识别 + AI 增强
        </div>
      </div>
    </div>

    <a-card :body-style="{ padding: '0 0 16px 0' }">
      <a-tabs v-model:active-key="activeTab" :tab-bar-style="{ padding: '0 20px', marginBottom: 0 }">
        <!-- Excel 导入 -->
        <a-tab-pane key="excel">
          <template #tab>
            <FileExcelOutlined />
            <span style="margin-left:6px">Excel 批量导入</span>
          </template>

          <div class="tab-body">
            <a-row :gutter="20">
              <a-col :xs="24" :md="14">
                <a-form-item label="解析模板（按品类）">
                  <a-select v-model:value="selectedTemplate" style="width:200px">
                    <a-select-option v-for="t in excelTemplates" :key="t.name" :value="t.name">
                      {{ t.name }}
                    </a-select-option>
                  </a-select>
                  <div style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:6px">
                    模板要求列：{{ excelTemplates.find(t => t.name === selectedTemplate)?.cols }}
                  </div>
                </a-form-item>

                <Upload.Dragger
                  v-bind="excelDraggerProps"
                  :file-list="excelFileList"
                  accept=".xlsx,.xls,.csv"
                  style="margin-top:8px"
                >
                  <p class="ant-upload-drag-icon">
                    <InboxOutlined />
                  </p>
                  <p class="ant-upload-text">点击或拖拽 Excel 到此区域上传</p>
                  <p class="ant-upload-hint">支持 .xlsx / .xls / .csv，单文件最多 2000 行</p>
                </Upload.Dragger>

                <div style="margin-top:16px;display:flex;gap:8px">
                  <a-button type="primary" :loading="excelImporting" @click="doExcelImport">
                    开始导入
                  </a-button>
                  <a-button @click="downloadTemplate">
                    <template #icon><DownloadOutlined /></template>
                    下载模板
                  </a-button>
                </div>

                <a-alert
                  v-if="excelResult"
                  type="success"
                  show-icon
                  style="margin-top:16px"
                  :message="`批次 ${excelResult.batch_id} 导入完成`"
                  :description="`新增 ${excelResult.imported} 条，跳过 ${excelResult.skipped} 条`"
                />
              </a-col>
              <a-col :xs="24" :md="10">
                <div class="template-list">
                  <div class="template-list__title">十大品类模板</div>
                  <div v-for="t in excelTemplates" :key="t.name" class="template-row">
                    <span class="template-row__name">{{ t.name }}</span>
                    <span class="template-row__cols">{{ t.cols }}</span>
                  </div>
                </div>
              </a-col>
            </a-row>
          </div>
        </a-tab-pane>

        <!-- OCR -->
        <a-tab-pane key="ocr">
          <template #tab>
            <ScanOutlined />
            <span style="margin-left:6px">OCR 扫描件入库</span>
          </template>

          <div class="tab-body">
            <!-- Project & supplier selection (needed for batch-confirm) -->
            <a-row :gutter="12" style="margin-bottom:14px;align-items:center">
              <a-col :xs="24" :sm="10">
                <div style="display:flex;align-items:center;gap:8px">
                  <span style="white-space:nowrap;font-size:13px;color:rgba(0,0,0,0.65)">所属项目</span>
                  <a-select
                    v-model:value="ocrProjectId"
                    :options="projectList.map(p => ({ value: p.id, label: p.name }))"
                    placeholder="选择项目（可选）"
                    allow-clear
                    style="flex:1"
                    size="small"
                  />
                </div>
              </a-col>
              <a-col :xs="24" :sm="10">
                <div style="display:flex;align-items:center;gap:8px">
                  <span style="white-space:nowrap;font-size:13px;color:rgba(0,0,0,0.65)">供应商名称</span>
                  <a-input
                    v-model:value="ocrSupplierName"
                    placeholder="OCR 识别后自动填入"
                    size="small"
                    style="flex:1"
                  />
                </div>
              </a-col>
            </a-row>

            <a-row :gutter="20">
              <a-col :xs="24" :md="10">
                <Upload.Dragger
                  v-bind="ocrDraggerProps"
                  :show-upload-list="false"
                >
                  <p class="ant-upload-drag-icon">
                    <ScanOutlined />
                  </p>
                  <p class="ant-upload-text">点击或拖拽 PDF / JPG / PNG 到此区域</p>
                  <p class="ant-upload-hint">系统将自动 OCR 识别 → AI 增强分类 → 可编辑确认</p>
                </Upload.Dragger>

                <div v-if="ocrPreviewUrl" style="margin-top:14px">
                  <img :src="ocrPreviewUrl" alt="扫描件预览" style="width:100%;max-height:300px;object-fit:contain;border:1px solid #f0f0f0;border-radius:6px" />
                </div>
                <div v-else-if="ocrFile" style="margin-top:14px;color:rgba(0,0,0,0.45);font-size:12px">
                  PDF 文件：{{ ocrFile.name }}
                </div>
              </a-col>
              <a-col :xs="24" :md="14">
                <a-spin :spinning="ocrParsing" tip="OCR 识别中…">
                  <a-spin :spinning="ocrEnhancing" tip="AI 增强中（分类 + 标准化）…">
                    <div v-if="!ocrResult && !ocrParsing && !ocrEnhancing" class="ocr-placeholder">
                      <RobotOutlined style="font-size:32px;color:rgba(0,0,0,0.25)" />
                      <div style="margin-top:8px">上传后将自动 OCR 识别 → AI 增强</div>
                    </div>
                    <template v-if="ocrResult">
                      <!-- AI enhance summary card -->
                      <div v-if="enhanceSummary" class="enhance-summary">
                        <ThunderboltOutlined style="color:#722ed1;margin-right:6px" />
                        <span class="enhance-summary__label">AI 增强</span>
                        <span class="enhance-summary__stat">识别 <b>{{ enhanceSummary.total }}</b> 行</span>
                        <a-divider type="vertical" />
                        <span class="enhance-summary__stat">
                          自动分类 <b>{{ enhanceSummary.categorized }}</b> 项
                        </span>
                        <a-divider type="vertical" />
                        <span class="enhance-summary__stat">
                          名称标准化
                          <b :style="enhanceSummary.renamed > 0 ? 'color:#d46b08' : ''">
                            {{ enhanceSummary.renamed }}
                          </b> 项
                        </span>
                        <a-divider type="vertical" />
                        <span class="enhance-summary__stat">
                          可对齐
                          <b :style="enhanceSummary.aligned > 0 ? 'color:#389e0d' : ''">
                            {{ enhanceSummary.aligned }}
                          </b> 项
                        </span>
                        <span v-if="enhanceSummary.errors > 0" style="margin-left:8px;color:#cf1322;font-size:12px">
                          ⚠ {{ enhanceSummary.errors }} 项未分类
                        </span>
                      </div>
                      <!-- Highlight legend -->
                      <div v-if="enhanceSummary" class="enhance-legend">
                        <span class="legend-item legend-item--yellow">■ 名称已标准化（悬停看原名）</span>
                        <span class="legend-item legend-item--blue">■ 品类标签</span>
                        <span class="legend-item legend-item--green">■ 可与已有报价对齐</span>
                      </div>
                      <!-- Supplier name reminder -->
                      <a-alert
                        v-if="!ocrSupplierName.trim()"
                        type="warning"
                        show-icon
                        message="请在上方填写供应商名称后再入库"
                        style="margin-bottom:10px"
                        :closable="false"
                      />
                      <ExtractionEditor
                        schema="quote"
                        :model-value="ocrResult as any"
                        :ai-mode="!!enhanceSummary"
                        confirm-label="确认入库"
                        @update:model-value="(v: any) => ocrResult = v"
                        @confirm="onOcrConfirm"
                      />
                    </template>
                  </a-spin>
                </a-spin>
              </a-col>
            </a-row>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>

  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.import-page {
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

.tab-body {
  padding: 16px 20px;
}

.template-list {
  background: #fafafa;
  border-radius: @border-radius-lg;
  padding: 14px 16px;

  &__title {
    font-size: 13px;
    color: @text-color-secondary;
    font-weight: 600;
    margin-bottom: 8px;
  }
}

.template-row {
  display: flex;
  padding: 6px 0;
  border-bottom: 1px dashed @border-color-split;
  font-size: 12px;

  &:last-child { border-bottom: none; }

  &__name {
    width: 80px;
    font-weight: 500;
    color: @text-color;
  }

  &__cols {
    flex: 1;
    color: @text-color-secondary;
  }
}

.ocr-placeholder {
  text-align: center;
  padding: 60px 0;
  color: @text-color-tertiary;
  font-size: 13px;
}

.enhance-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
  background: #f9f0ff;
  border: 1px solid #d3adf7;
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 8px;
  font-size: 13px;

  &__label {
    font-weight: 600;
    color: #722ed1;
    margin-right: 8px;
  }

  &__stat {
    color: @text-color;
    b { font-weight: 600; }
  }
}

.enhance-legend {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11px;
  color: @text-color-secondary;
  margin-bottom: 8px;
  padding: 0 2px;
}

.legend-item {
  &--yellow { color: #d48806; }
  &--blue { color: #1677ff; }
  &--green { color: #389e0d; }
}
</style>
