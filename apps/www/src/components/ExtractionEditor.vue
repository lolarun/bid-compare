<script setup lang="ts">
import { computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined, CheckCircleOutlined, SwapOutlined, LinkOutlined, FileSearchOutlined } from '@ant-design/icons-vue'

// Arithmetic conflict threshold: 11.5% = 13%/113% (VAT misread) + 1% rounding.
// Rows at ≤ this threshold are VAT-level warnings; rows above are hard errors.
const ARITHMETIC_HARD_CONFLICT_THRESHOLD = 0.125

/**
 * Generic editable extraction-result table.
 *
 * Audit-driven design (Phase 3 → audit-fix D):
 *
 * - **Fully controlled component**: no internal `rows` ref. We bind to
 *   props.modelValue directly through a computed getter/setter. This avoids
 *   the bug where the parent re-emitting modelValue clobbered the user's
 *   in-progress edits (and broke IME composition for Chinese input).
 *
 * - **Stable row keys**: each row gets a stable `_rid` (assigned on first
 *   render or on add). Deleting a row no longer shifts every other row's
 *   key, so AntdV doesn't re-mount the input components.
 *
 * - **Explicit reactivity**: a-input-number uses `:value` + `@update:value`
 *   instead of `v-model` on a cast lvalue, so the change fires immediately
 *   (not just on blur).
 *
 * - **Required-field validation**: empty material/name rows are filtered
 *   before emit('confirm') so the backend never sees blank rows.
 *
 * - **Delete confirmation**: removeRow uses Modal.confirm to prevent
 *   accidental data loss.
 */

type SchemaType = 'tender' | 'quote'

// _rid is an internal-only field; consumers ignore it.
interface RowBase { _rid?: number }

interface TenderRow extends RowBase {
  name: string
  category: string
  spec: string
  unit: string
  quantity: number | null
  remark: string
  extended_attrs?: Record<string, unknown>
}
interface QuoteRow extends RowBase {
  material: string
  spec: string
  brand: string
  unit: string
  qty: number | null
  unit_price: number | null
  unit_price_excl_tax: number | null
  total_price: number | null
  tax_rate: number | null
  // 价格口径桥接字段（§4/§9）：显式声明，避免编辑器行展开 {...r} 静默丢失。
  unit_price_incl_tax?: number | null
  total_price_incl_tax?: number | null
  total_price_excl_tax?: number | null
  tax_amount?: number | null
  price_basis?: string
  effective_unit_price?: number | null
  effective_total_price?: number | null
  validation_flags?: string[]
  raw_qty?: number | null
  suggested_qty?: number | null
  material_type?: string
  remark: string
  // AI-enhanced fields (populated after /api/intake/enhance)
  category?: string
  original_name?: string
  name_note?: string
  alignment_note?: string
  matched_material_id?: number | null
  // hidden fields — not edited in UI but must survive the round-trip to
  // batch-confirm so canonical / validation_warning / source_ref reach
  // anchor-match and LLM supplier-fill intact. Declared explicitly so the
  // editor's row-spread doesn't silently drop them on a contract change.
  canonical?: Record<string, unknown>
  validation_warning?: string
  source_ref?: Record<string, unknown>
  normalized_material?: string
  ocr_correction_reason?: string
  standard_name?: string
  standard_spec?: string
}

type Row = TenderRow | QuoteRow

const props = withDefaults(defineProps<{
  schema: SchemaType
  modelValue: Row[]
  confirmLabel?: string
  showActions?: boolean
  /** Enable AI-highlight mode: yellow for renamed, blue tag for category, green for aligned */
  aiMode?: boolean
}>(), {
  confirmLabel: '确认入库',
  showActions: true,
  aiMode: false,
})

const emit = defineEmits<{
  (e: 'update:modelValue', rows: Row[]): void
  (e: 'confirm', rows: Row[]): void
}>()

// ─── Stable row-id assignment ────────────────────────────────────────────
let nextRid = 1
function ensureRid(row: Row): Row {
  if (typeof row._rid !== 'number') {
    Object.defineProperty(row, '_rid', { value: nextRid++, enumerable: true, writable: true })
  }
  return row
}

// Fully controlled: rows is a computed proxy over props.modelValue.
const rows = computed<Row[]>({
  get: () => props.modelValue.map(ensureRid),
  set: (v) => emit('update:modelValue', v),
})

function updateField(rid: number, field: string, value: unknown) {
  const next = rows.value.map((r) => {
    if (r._rid !== rid) return r
    return { ...r, [field]: value } as Row
  })
  emit('update:modelValue', next)
}

function addRow() {
  const blank: Row = props.schema === 'tender'
    ? { _rid: nextRid++, name: '', category: '', spec: '', unit: '', quantity: null, remark: '', extended_attrs: {} }
    : {
      _rid: nextRid++, material: '', spec: '', brand: '', unit: '',
      qty: null, unit_price: null, unit_price_excl_tax: null,
      total_price: null, tax_rate: null, remark: '',
    }
  emit('update:modelValue', [...rows.value, blank])
}

function removeRow(rid: number) {
  Modal.confirm({
    title: '删除此行？',
    content: '删除后无法撤销，需手动重新录入。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: () => {
      emit('update:modelValue', rows.value.filter((r) => r._rid !== rid))
    },
  })
}

function onConfirm() {
  if (props.schema === 'quote' && hasHardConflicts.value) {
    message.error('存在算术冲突（数量×单价与总价偏差>12.5%），请先修正红色行再入库')
    return
  }
  // Strip _rid and filter empty rows (no material/name)
  const cleaned = rows.value
    .filter((r) => {
      if (props.schema === 'tender') {
        return ((r as TenderRow).name || '').trim().length > 0
      }
      return ((r as QuoteRow).material || '').trim().length > 0
    })
    .map((r) => {
      const copy: Record<string, unknown> = { ...r }
      delete copy._rid
      return copy as unknown as Row
    })
  if (cleaned.length === 0) {
    message.warning('至少需要 1 行有效数据（材料名称必填）')
    return
  }
  if (cleaned.length < rows.value.length) {
    message.info(`已自动忽略 ${rows.value.length - cleaned.length} 条空行`)
  }
  emit('confirm', cleaned)
}

// ─── Column configs ─────────────────────────────────────────────────────
const tenderColumns = [
  { title: '材料名称', dataIndex: 'name' },
  { title: '品类', dataIndex: 'category', width: 100 },
  { title: '规格', dataIndex: 'spec', width: 140 },
  { title: '单位', dataIndex: 'unit', width: 70 },
  { title: '数量', dataIndex: 'quantity', width: 90, align: 'right' as const },
  { title: '备注', dataIndex: 'remark' },
  { title: '技术参数', dataIndex: 'extended_attrs', width: 180 },
]

const quoteColumns = [
  { title: '材料名称', dataIndex: 'material' },
  { title: '规格', dataIndex: 'spec', width: 130 },
  { title: '品牌', dataIndex: 'brand', width: 110 },
  { title: '单位', dataIndex: 'unit', width: 60 },
  { title: '数量', dataIndex: 'qty', width: 80, align: 'right' as const },
  { title: '口径', dataIndex: '_basis', width: 74, align: 'center' as const },
  { title: '比价单价', dataIndex: 'unit_price', width: 110, align: 'right' as const },
  { title: '不含税单价', dataIndex: 'unit_price_excl_tax', width: 110, align: 'right' as const },
  { title: '比价合价', dataIndex: 'total_price', width: 110, align: 'right' as const },
  { title: '计算合价', dataIndex: '_calc', width: 110, align: 'right' as const },
  { title: '差异', dataIndex: '_dev', width: 72, align: 'center' as const },
  { title: '税率', dataIndex: 'tax_rate', width: 70, align: 'right' as const },
  { title: '来源页', dataIndex: '_source', width: 68, align: 'center' as const },
  { title: '备注', dataIndex: 'remark' },
]

const numericFields = new Set([
  'quantity', 'qty', 'unit_price', 'unit_price_excl_tax', 'total_price', 'tax_rate',
])

/** 比价有效单价：人工编辑过的 unit_price 优先，否则取桥接 effective（§4/§9）。
 *  与后端 batch-confirm 的「人工值优先」语义一致：unit_price ?? effective_unit_price。 */
function effUnit(row: Row): number | null {
  const q = row as QuoteRow
  return q.unit_price ?? q.effective_unit_price ?? null
}
/** 比价有效合价：total_price（人工）优先，否则 effective_total_price。 */
function effTotal(row: Row): number | null {
  const q = row as QuoteRow
  return q.total_price ?? q.effective_total_price ?? null
}
/** 价格口径标签：含税 / 不含税 / 含税(双) / 未注明 / 未知。 */
const PRICE_BASIS_LABELS: Record<string, { text: string; color: string }> = {
  incl_tax: { text: '含税', color: 'green' },
  excl_tax: { text: '不含税', color: 'orange' },
  dual_tax: { text: '含税(双)', color: 'green' },
  unspecified: { text: '未注明', color: 'default' },
  unknown: { text: '未知', color: 'red' },
}
function priceBasisTag(row: Row): { text: string; color: string } | null {
  if (props.schema !== 'quote') return null
  const b = (row as QuoteRow).price_basis
  if (!b) return null
  return PRICE_BASIS_LABELS[b] ?? { text: b, color: 'default' }
}

function calcPrice(row: Row): number | null {
  if (props.schema !== 'quote') return null
  const q = row as QuoteRow
  const u = effUnit(q)
  if (u == null || q.qty == null) return null
  return Math.round(u * q.qty * 100) / 100
}

function priceDeviation(row: Row): number | null {
  if (props.schema !== 'quote') return null
  const q = row as QuoteRow
  const u = effUnit(q)
  const t = effTotal(q)
  if (u == null || q.qty == null || t == null) return null
  if (t === 0) return null
  const expected = u * q.qty
  const denom = Math.max(Math.abs(t), Math.abs(expected))
  return denom === 0 ? null : Math.abs(expected - t) / denom
}

function isHardConflict(row: Row): boolean {
  const dev = priceDeviation(row)
  return dev !== null && dev > ARITHMETIC_HARD_CONFLICT_THRESHOLD
}

const hasHardConflicts = computed<boolean>(() => {
  if (props.schema !== 'quote') return false
  return rows.value.some(r => isHardConflict(r))
})

function sourceRefLabel(row: Row): string {
  if (props.schema !== 'quote') return ''
  const q = row as QuoteRow
  if (!q.source_ref) return ''
  const s = q.source_ref as Record<string, unknown>
  if (s.page) return `P${s.page}`
  if (s.location) return String(s.location)
  return JSON.stringify(s).slice(0, 20)
}

/**
 * Detect likely field mis-assignment and return a correction suggestion.
 * Common OCR error: LLM puts total_price into unit_price field.
 * E.g. qty=17, unit_price=1802, total_price=1802 → unit_price should be 106.
 */
interface CorrectionSuggestion {
  type: 'swap_total_to_unit' | 'unit_is_total'
  message: string
  suggestedUnitPrice: number
}

function detectCorrection(row: Row): CorrectionSuggestion | null {
  if (props.schema !== 'quote') return null
  const q = row as QuoteRow
  // 用比价有效价（effective fallback），不再固定取 unit_price
  const u = effUnit(q)
  const t = effTotal(q)
  if (u == null || q.qty == null || q.qty <= 1) return null

  // Case 1: 有效单价 ≈ 有效合价 and qty > 1 → 单价疑似填成了合价
  if (t != null && t > 0) {
    const ratio = u / t
    if (ratio > 0.95 && ratio < 1.05) {
      const corrected = Math.round((t / q.qty) * 100) / 100
      return {
        type: 'unit_is_total',
        message: `单价疑似为合价，建议修正为 ${corrected}`,
        suggestedUnitPrice: corrected,
      }
    }
  }

  // Case 2: 无有效合价但 单价×数量 异常大（单价疑似是合价）
  if (t == null && u > 0) {
    const divided = u / q.qty
    if (q.qty >= 2 && divided > 1 && Number.isFinite(divided)) {
      const rounded = Math.round(divided * 100) / 100
      if (u >= q.qty * 2 && rounded !== u) {
        const remainder = u % q.qty
        if (remainder === 0 || Math.abs(remainder / u) < 0.01) {
          return {
            type: 'swap_total_to_unit',
            message: `单价可能是合价，建议：单价=${rounded}，总价=${u}`,
            suggestedUnitPrice: rounded,
          }
        }
      }
    }
  }

  return null
}

function applyCorrection(rid: number) {
  const row = rows.value.find((r) => r._rid === rid)
  if (!row) return
  const suggestion = detectCorrection(row)
  if (!suggestion) return
  const q = row as QuoteRow
  const next = rows.value.map((r) => {
    if (r._rid !== rid) return r
    if (suggestion.type === 'unit_is_total') {
      return { ...r, unit_price: suggestion.suggestedUnitPrice } as Row
    }
    if (suggestion.type === 'swap_total_to_unit') {
      return { ...r, unit_price: suggestion.suggestedUnitPrice, total_price: q.unit_price } as Row
    }
    return r
  })
  emit('update:modelValue', next)
  message.success('已自动修正单价')
}

const columns = computed(() => {
  const base = props.schema === 'tender' ? tenderColumns : quoteColumns
  const cols: Array<Record<string, unknown>> = []
  // In AI mode, prepend a read-only category tag column
  if (props.aiMode && props.schema === 'quote') {
    cols.push({ title: '分类', dataIndex: '_category', width: 80, align: 'center' })
  }
  cols.push(...base)
  // In AI mode, add alignment indicator column
  if (props.aiMode && props.schema === 'quote') {
    cols.push({ title: '', dataIndex: '_align', width: 36, align: 'center' })
  }
  if (props.showActions) {
    cols.push({ title: '操作', dataIndex: '_actions', width: 60, fixed: 'right' })
  }
  return cols
})
</script>

<template>
  <div class="extraction-editor">
    <a-table
      :columns="columns"
      :data-source="rows"
      :pagination="false"
      :row-key="(r: Row) => r._rid as number"
      size="middle"
      bordered
      :scroll="{ x: schema === 'quote' ? 1400 : undefined }"
      :row-class-name="(record: Row) => isHardConflict(record) ? 'row-arithmetic-conflict' : ''"
    >
      <template #bodyCell="{ column, record }">
        <!-- AI mode: category tag column -->
        <template v-if="column.dataIndex === '_category'">
          <a-tag
            v-if="(record as QuoteRow).category"
            color="blue"
            style="font-size:11px;margin:0;padding:0 4px;line-height:18px"
          >{{ (record as QuoteRow).category }}</a-tag>
          <span v-else style="color:rgba(0,0,0,0.25);font-size:11px">—</span>
        </template>

        <!-- AI mode: alignment indicator column -->
        <template v-else-if="column.dataIndex === '_align'">
          <a-tooltip
            v-if="(record as QuoteRow).alignment_note"
            :title="(record as QuoteRow).alignment_note"
          >
            <LinkOutlined style="color:#52c41a;font-size:15px;cursor:default" />
          </a-tooltip>
        </template>

        <template v-else-if="column.dataIndex === '_calc'">
          <span v-if="calcPrice(record as Row) !== null" :style="isHardConflict(record as Row) ? 'color:#ff4d4f;font-weight:600' : ''">
            {{ calcPrice(record as Row)!.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }}
          </span>
          <span v-else style="color:rgba(0,0,0,0.25)">—</span>
        </template>

        <template v-else-if="column.dataIndex === '_dev'">
          <template v-if="priceDeviation(record as Row) !== null">
            <a-tooltip
              v-if="detectCorrection(record as Row)"
              :title="detectCorrection(record as Row)!.message + '（点击修正）'"
            >
              <span style="color:#ff4d4f;cursor:pointer;font-weight:600"
                @click="applyCorrection((record as Row)._rid as number)">
                {{ ((priceDeviation(record as Row) as number) * 100).toFixed(1) }}%
                <SwapOutlined style="font-size:12px" />
              </span>
            </a-tooltip>
            <span v-else-if="isHardConflict(record as Row)"
              style="color:#ff4d4f;font-weight:600">
              {{ ((priceDeviation(record as Row) as number) * 100).toFixed(1) }}%
            </span>
            <span v-else-if="(priceDeviation(record as Row) as number) > 0.01"
              style="color:#faad14">
              {{ ((priceDeviation(record as Row) as number) * 100).toFixed(1) }}%
            </span>
            <span v-else style="color:rgba(0,0,0,0.25)">—</span>
          </template>
          <span v-else style="color:rgba(0,0,0,0.25)">—</span>
        </template>

        <template v-else-if="column.dataIndex === '_source'">
          <a-tooltip v-if="sourceRefLabel(record as Row)" :title="`原文位置：${sourceRefLabel(record as Row)}`">
            <span style="font-size:11px;color:#1677ff;cursor:default">
              <FileSearchOutlined /> {{ sourceRefLabel(record as Row) }}
            </span>
          </a-tooltip>
          <span v-else style="color:rgba(0,0,0,0.2);font-size:11px">—</span>
        </template>

        <template v-else-if="column.dataIndex === '_warn'">
          <!-- kept for backward compat — now _dev column does the heavy lifting -->
        </template>
        <template v-else-if="column.dataIndex === 'extended_attrs'">
          <div v-if="(record as TenderRow).extended_attrs && Object.keys((record as TenderRow).extended_attrs!).length > 0" class="ext-attrs">
            <a-tag
              v-for="(val, key) in (record as TenderRow).extended_attrs"
              :key="key"
              color="blue"
              size="small"
            >{{ key }}={{ val }}</a-tag>
          </div>
          <span v-else style="color:rgba(0,0,0,0.25);font-size:12px">—</span>
        </template>
        <template v-else-if="column.dataIndex === '_actions'">
          <a-button
            type="link"
            danger
            size="small"
            :aria-label="'删除此行'"
            @click="removeRow((record as Row)._rid as number)"
          >
            <template #icon><DeleteOutlined /></template>
          </a-button>
        </template>

        <!-- 价格口径标签：含税 / 不含税 / 未注明 / 未知 -->
        <template v-else-if="column.dataIndex === '_basis'">
          <a-tag
            v-if="priceBasisTag(record as Row)"
            :color="priceBasisTag(record as Row)!.color"
            style="font-size:11px;margin:0;padding:0 5px;line-height:18px"
          >{{ priceBasisTag(record as Row)!.text }}</a-tag>
          <span v-else style="color:rgba(0,0,0,0.25);font-size:11px">—</span>
        </template>

        <!-- Number cells: explicit :value + @update:value triggers emit immediately.
             比价单价/比价合价显示有效价（effective fallback）；编辑写入 unit_price/total_price
             作为人工覆盖（与后端 batch-confirm「人工值优先」一致）。 -->
        <template v-else-if="numericFields.has(String(column.dataIndex))">
          <a-input-number
            :value="column.dataIndex === 'unit_price'
              ? effUnit(record as Row)
              : column.dataIndex === 'total_price'
                ? effTotal(record as Row)
                : (record as Record<string, number | null>)[column.dataIndex as string]"
            :step="column.dataIndex === 'tax_rate' ? 0.01 : 0.1"
            :status="column.dataIndex === 'unit_price' && effUnit(record as Row) == null ? 'error' : undefined"
            style="width:100%"
            size="small"
            @update:value="(v: number | null) => updateField((record as Row)._rid as number, column.dataIndex as string, v)"
          />
        </template>

        <!-- Material cell: yellow highlight + tooltip when AI renamed it -->
        <template v-else-if="column.dataIndex === 'material' && aiMode && schema === 'quote'">
          <a-tooltip
            v-if="(record as QuoteRow).original_name && (record as QuoteRow).original_name !== (record as QuoteRow).material"
            :title="`原始名称：${(record as QuoteRow).original_name}${(record as QuoteRow).name_note ? '（' + (record as QuoteRow).name_note + '）' : ''}`"
          >
            <a-input
              :value="(record as Record<string, string>)[column.dataIndex as string]"
              size="small"
              placeholder="材料名称"
              class="ai-renamed-cell"
              @update:value="(v: string) => updateField((record as Row)._rid as number, column.dataIndex as string, v)"
            />
          </a-tooltip>
          <a-input
            v-else
            :value="(record as Record<string, string>)[column.dataIndex as string]"
            size="small"
            placeholder="材料名称"
            @update:value="(v: string) => updateField((record as Row)._rid as number, column.dataIndex as string, v)"
          />
        </template>

        <!-- Default: text cell -->
        <template v-else>
          <a-input
            :value="(record as Record<string, string>)[column.dataIndex as string]"
            size="small"
            :placeholder="String(column.title)"
            @update:value="(v: string) => updateField((record as Row)._rid as number, column.dataIndex as string, v)"
          />
        </template>
      </template>
    </a-table>

    <div class="extraction-editor__footer">
      <a-button @click="addRow">
        <template #icon><PlusOutlined /></template>
        新增行
      </a-button>
      <a-tooltip
        v-if="showActions"
        :title="hasHardConflicts ? '存在算术冲突行（红色标注），请先修正再入库' : ''"
      >
        <a-button
          type="primary"
          :disabled="hasHardConflicts"
          @click="onConfirm"
        >
          <template #icon><CheckCircleOutlined /></template>
          {{ confirmLabel }}
          <template v-if="hasHardConflicts">
            （{{ rows.filter(r => isHardConflict(r)).length }} 行冲突）
          </template>
        </a-button>
      </a-tooltip>
    </div>
  </div>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.extraction-editor {
  &__footer {
    display: flex;
    justify-content: space-between;
    margin-top: 12px;
    gap: 8px;
  }
}

.ext-attrs {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  :deep(.ant-tag) {
    font-size: 11px;
    line-height: 18px;
    margin: 0;
  }
}

// Arithmetic conflict row: red tint
:deep(.row-arithmetic-conflict) {
  background-color: #fff2f0 !important;
  > td {
    background-color: #fff2f0 !important;
  }
}

// AI highlight: yellow background on renamed material cells
.ai-renamed-cell {
  :deep(.ant-input) {
    background-color: #fffbe6;
    border-color: #faad14;
  }
  :deep(.ant-input:focus) {
    background-color: #fffbe6;
    border-color: #d48806;
    box-shadow: 0 0 0 2px rgba(250, 173, 20, 0.2);
  }
}
</style>
