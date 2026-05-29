<script setup lang="ts">
import { computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined, CheckCircleOutlined, WarningOutlined, SwapOutlined } from '@ant-design/icons-vue'

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
  remark: string
}

type Row = TenderRow | QuoteRow

const props = withDefaults(defineProps<{
  schema: SchemaType
  modelValue: Row[]
  confirmLabel?: string
  showActions?: boolean
}>(), {
  confirmLabel: '确认入库',
  showActions: true,
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
  { title: '含税单价', dataIndex: 'unit_price', width: 110, align: 'right' as const },
  { title: '不含税单价', dataIndex: 'unit_price_excl_tax', width: 110, align: 'right' as const },
  { title: '总价', dataIndex: 'total_price', width: 110, align: 'right' as const },
  { title: '税率', dataIndex: 'tax_rate', width: 80, align: 'right' as const },
  { title: '备注', dataIndex: 'remark' },
]

const numericFields = new Set([
  'quantity', 'qty', 'unit_price', 'unit_price_excl_tax', 'total_price', 'tax_rate',
])

function priceDeviation(row: Row): number | null {
  if (props.schema !== 'quote') return null
  const q = row as QuoteRow
  if (q.unit_price == null || q.qty == null || q.total_price == null) return null
  if (q.total_price === 0) return null
  const expected = q.unit_price * q.qty
  return Math.abs(expected - q.total_price) / q.total_price
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
  if (q.unit_price == null || q.qty == null || q.qty <= 1) return null

  // Case 1: unit_price ≈ total_price and qty > 1 → unit_price is likely the total
  if (q.total_price != null && q.total_price > 0) {
    const ratio = q.unit_price / q.total_price
    if (ratio > 0.95 && ratio < 1.05) {
      const corrected = Math.round((q.total_price / q.qty) * 100) / 100
      return {
        type: 'unit_is_total',
        message: `单价疑似为合价，建议修正为 ${corrected}`,
        suggestedUnitPrice: corrected,
      }
    }
  }

  // Case 2: no total_price but unit_price * qty seems unreasonably high
  // (unit_price looks like it might be a total — check if dividing by qty gives a round number)
  if (q.total_price == null && q.unit_price > 0) {
    const divided = q.unit_price / q.qty
    // If dividing gives a clean result and original is much larger than divided
    if (q.qty >= 2 && divided > 1 && Number.isFinite(divided)) {
      const rounded = Math.round(divided * 100) / 100
      // Only suggest if the ratio is significant (unit_price at least 2x what per-unit would be)
      if (q.unit_price >= q.qty * 2 && rounded !== q.unit_price) {
        // Heuristic: if unit_price ends in round multiples of qty, it's likely a total
        const remainder = q.unit_price % q.qty
        if (remainder === 0 || Math.abs(remainder / q.unit_price) < 0.01) {
          return {
            type: 'swap_total_to_unit',
            message: `单价可能是合价，建议：单价=${rounded}，总价=${q.unit_price}`,
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
  const cols: Array<Record<string, unknown>> = [...base]
  if (props.schema === 'quote') {
    cols.push({ title: '', dataIndex: '_warn', width: 36, align: 'center' })
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
      :scroll="{ x: schema === 'quote' ? 1200 : undefined }"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.dataIndex === '_warn'">
          <!-- Correction suggestion (clickable auto-fix) -->
          <a-tooltip
            v-if="detectCorrection(record as Row)"
            :title="detectCorrection(record as Row)!.message + '（点击修正）'"
          >
            <SwapOutlined
              style="color:#ff4d4f;font-size:16px;cursor:pointer"
              @click="applyCorrection((record as Row)._rid as number)"
            />
          </a-tooltip>
          <!-- Simple deviation warning (no auto-fix available) -->
          <a-tooltip
            v-else-if="priceDeviation(record as Row) !== null && (priceDeviation(record as Row) as number) > 0.01"
            :title="`单价×数量与总价偏差 ${((priceDeviation(record as Row) as number) * 100).toFixed(1)}%，请核对`"
          >
            <WarningOutlined style="color:#faad14;font-size:16px" />
          </a-tooltip>
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

        <!-- Number cells: explicit :value + @update:value triggers emit immediately -->
        <template v-else-if="numericFields.has(String(column.dataIndex))">
          <a-input-number
            :value="(record as Record<string, number | null>)[column.dataIndex as string]"
            :step="column.dataIndex === 'tax_rate' ? 0.01 : 0.1"
            :status="column.dataIndex === 'unit_price' && (record as Record<string, number | null>)[column.dataIndex as string] == null ? 'error' : undefined"
            style="width:100%"
            size="small"
            @update:value="(v: number | null) => updateField((record as Row)._rid as number, column.dataIndex as string, v)"
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
      <a-button v-if="showActions" type="primary" @click="onConfirm">
        <template #icon><CheckCircleOutlined /></template>
        {{ confirmLabel }}
      </a-button>
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
</style>
