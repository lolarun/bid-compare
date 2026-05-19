<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { message, Upload, type UploadProps } from 'ant-design-vue'
import {
  InboxOutlined,
  FileExcelOutlined,
  ScanOutlined,
  DownloadOutlined,
  RobotOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons-vue'
import { quoteApi } from '@/api'

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
    // 检测未知品牌（mock 触发）
    if (data.errors?.some((e) => String((e as { code?: string }).code).includes('unknown_brand'))) {
      brandTierVisible.value = true
    }
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
const ocrResult = ref<Array<{
  material: string; spec: string; brand: string; unit: string; qty: number; price: number;
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

async function parseOcr() {
  ocrParsing.value = true
  // 实际接入：POST /api/quotes/ocr
  // 此处用 mock 数据演示流程
  setTimeout(() => {
    ocrResult.value = [
      { material: 'DN100 无缝钢管', spec: 'Q235', brand: '宝钢', unit: '米', qty: 200, price: 71.0 },
      { material: '配电箱 XL-21', spec: '600×800×200', brand: '正泰电器', unit: '台', qty: 8, price: 1180 },
      { material: 'PPR 给水管 De32', spec: '3.6mm壁厚', brand: '伟星新材', unit: '米', qty: 500, price: 12.80 },
    ]
    ocrParsing.value = false
    message.success('OCR 解析完成，可编辑后确认入库')
    // mock 触发新品牌弹窗
    brandTierVisible.value = true
    unknownBrands.value = ['正泰电器']
  }, 1200)
}

const ocrColumns = [
  { title: '材料名称', dataIndex: 'material' },
  { title: '规格', dataIndex: 'spec', width: 130 },
  { title: '品牌', dataIndex: 'brand', width: 120 },
  { title: '单位', dataIndex: 'unit', width: 60 },
  { title: '数量', dataIndex: 'qty', width: 80, align: 'right' as const },
  { title: '单价', dataIndex: 'price', width: 100, align: 'right' as const,
    customRender: ({ text }: { text: number }) => `¥${text.toFixed(2)}` },
]

async function confirmOcr() {
  message.success(`已入库 ${ocrResult.value?.length || 0} 条记录`)
  ocrResult.value = null
  ocrFile.value = null
  ocrPreviewUrl.value = null
}

// ─── 品牌档位弹窗 ────────────────────────────────────────────────────────
const brandTierVisible = ref(false)
const unknownBrands = ref<string[]>(['正泰电器'])
const brandTierForm = ref<Record<string, string>>({ '正泰电器': '一档' })

async function saveBrandTiers() {
  // 实际接入：POST /api/brand-tiers （逐条或批量）
  message.success('品牌档位已写入')
  brandTierVisible.value = false
}
</script>

<template>
  <div class="import-page">
    <!-- 标题 -->
    <div class="import-page__header">
      <div>
        <h1 class="import-page__title">采购价格导入</h1>
        <div class="import-page__subtitle">
          Excel 批量导入 · PDF/JPG 自动 OCR 入库 · 首次品牌档位弹窗写入
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
                  <a-button>
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
                  <p class="ant-upload-hint">系统将自动 OCR 识别并提取报价表结构</p>
                </Upload.Dragger>

                <div v-if="ocrPreviewUrl" style="margin-top:14px">
                  <img :src="ocrPreviewUrl" alt="扫描件预览" style="width:100%;max-height:300px;object-fit:contain;border:1px solid #f0f0f0;border-radius:6px" />
                </div>
                <div v-else-if="ocrFile" style="margin-top:14px;color:rgba(0,0,0,0.45);font-size:12px">
                  PDF 文件：{{ ocrFile.name }}
                </div>
              </a-col>
              <a-col :xs="24" :md="14">
                <a-spin :spinning="ocrParsing">
                  <div v-if="!ocrResult" class="ocr-placeholder">
                    <RobotOutlined style="font-size:32px;color:rgba(0,0,0,0.25)" />
                    <div style="margin-top:8px">上传后将自动开始 OCR 识别</div>
                  </div>
                  <template v-else>
                    <a-alert
                      type="info"
                      show-icon
                      message="OCR 解析完成"
                      description="请核对字段后点击确认入库；不一致的可直接在表格中编辑修改。"
                      style="margin-bottom:12px"
                    />
                    <a-table
                      :columns="ocrColumns"
                      :data-source="ocrResult"
                      :pagination="false"
                      :row-key="(r: { material: string }) => r.material"
                      size="middle"
                    />
                    <div style="margin-top:12px;text-align:right">
                      <a-button type="primary" @click="confirmOcr">
                        <template #icon><CheckCircleOutlined /></template>
                        确认入库
                      </a-button>
                    </div>
                  </template>
                </a-spin>
              </a-col>
            </a-row>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- 品牌档位弹窗 -->
    <a-modal
      v-model:open="brandTierVisible"
      title="发现新品牌，请录入档位"
      :width="520"
      @ok="saveBrandTiers"
    >
      <a-alert
        type="warning"
        show-icon
        message="以下品牌未在档位映射表中，请指定档位后继续入库"
        style="margin-bottom:12px"
      />
      <a-form layout="horizontal" :label-col="{ span: 8 }">
        <a-form-item v-for="brand in unknownBrands" :key="brand" :label="brand">
          <a-radio-group v-model:value="brandTierForm[brand]">
            <a-radio-button value="一档">一档</a-radio-button>
            <a-radio-button value="二档">二档</a-radio-button>
            <a-radio-button value="三档">三档</a-radio-button>
          </a-radio-group>
        </a-form-item>
      </a-form>
      <div style="font-size:12px;color:rgba(0,0,0,0.45);margin-top:8px">
        档位写入后可在「系统设置 → 品牌档位映射」中维护
      </div>
    </a-modal>
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
</style>
