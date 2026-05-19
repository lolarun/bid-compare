<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { PlusOutlined, DeleteOutlined, CheckCircleOutlined } from '@ant-design/icons-vue'

/**
 * Generic editable extraction-result table.
 *
 * Props:
 * - schema: 'tender' | 'quote'
 * - rows:  pre-filled rows from the LLM
 *
 * Emits:
 * - confirm(rows): user clicked "确认入库"
 * - change(rows):  any inline edit (debounced)
 */

type SchemaType = 'tender' | 'quote'

interface TenderRow {
  name: string
  category: string
  spec: string
  unit: string
  quantity: number | null
  remark: string
}
interface QuoteRow {
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

const rows = ref<Row[]>([...props.modelValue])
watch(() => props.modelValue, (v) => { rows.value = [...v] })

function emitUpdate() {
  emit('update:modelValue', [...rows.value])
}

function addRow() {
  if (props.schema === 'tender') {
    rows.value.push({ name: '', category: '', spec: '', unit: '', quantity: null, remark: '' })
  } else {
    rows.value.push({
      material: '', spec: '', brand: '', unit: '',
      qty: null, unit_price: null, unit_price_excl_tax: null,
      total_price: null, tax_rate: null, remark: '',
    })
  }
  emitUpdate()
}

function removeRow(idx: number) {
  rows.value.splice(idx, 1)
  emitUpdate()
}

function onConfirm() {
  if (rows.value.length === 0) {
    message.warning('至少需要 1 行数据')
    return
  }
  emit('confirm', [...rows.value])
}

// Column configs — keep cells editable inline via slot scope
const tenderColumns = [
  { title: '材料名称', dataIndex: 'name' },
  { title: '品类', dataIndex: 'category', width: 100 },
  { title: '规格', dataIndex: 'spec', width: 140 },
  { title: '单位', dataIndex: 'unit', width: 70 },
  { title: '数量', dataIndex: 'quantity', width: 90, align: 'right' as const },
  { title: '备注', dataIndex: 'remark' },
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

const columns = computed(() => {
  const base = props.schema === 'tender' ? tenderColumns : quoteColumns
  if (!props.showActions) return base
  return [...base, { title: '操作', dataIndex: '_actions', width: 60, fixed: 'right' as const }]
})
</script>

<template>
  <div class="extraction-editor">
    <a-table
      :columns="columns"
      :data-source="rows"
      :pagination="false"
      :row-key="(_r: Row, i: number) => i"
      size="middle"
      bordered
      :scroll="{ x: schema === 'quote' ? 1200 : undefined }"
    >
      <template #bodyCell="{ column, index, record }">
        <template v-if="column.dataIndex === '_actions'">
          <a-button type="link" danger size="small" @click="removeRow(index)">
            <template #icon><DeleteOutlined /></template>
          </a-button>
        </template>

        <!-- Number cells -->
        <template v-else-if="['quantity','qty','unit_price','unit_price_excl_tax','total_price','tax_rate'].includes(String(column.dataIndex))">
          <a-input-number
            v-model:value="(record as Record<string, number | null>)[column.dataIndex as string]"
            :step="column.dataIndex === 'tax_rate' ? 0.01 : 0.1"
            style="width:100%"
            size="small"
            @change="emitUpdate"
          />
        </template>

        <!-- Default: text cell -->
        <template v-else>
          <a-input
            v-model:value="(record as Record<string, string>)[column.dataIndex as string]"
            size="small"
            :placeholder="String(column.title)"
            @change="emitUpdate"
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
</style>
