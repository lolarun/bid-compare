<!--
  design/27 §5/§10 步骤2 —— Univer 版报价行编辑器（ExtractionEditor 的候选
  替代品，尚未接入生产工作台——那是步骤3/4 的范围，这里先把组件本身做对）。

  只调用 `@/univer/quoteGridController` 导出的函数，不 import 任何
  `@univerjs/*`、不碰 Univer 对象——复核意见要求的隔离边界在这个文件严格
  遵守，往后要换 AG Grid 只改 controller 那一个文件。
-->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  mountQuoteGrid,
  type DoubtMark,
  type QuoteGridColumn,
  type QuoteGridHandle,
} from '@/univer/quoteGridController'

const props = defineProps<{
  modelValue: Record<string, unknown>[]
  columns: QuoteGridColumn[]
  doubtMarks?: DoubtMark[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', rows: Record<string, unknown>[]): void
}>()

const containerEl = ref<HTMLElement | null>(null)
let handle: QuoteGridHandle | null = null
// 编辑触发的 emit 会让 modelValue 变化，反过来又会被下面的 watch 捕到——
// 不加这个哨兵会互相触发成环（每次编辑都整表重新装载一遍，光标跳飞）。
let emittingFromEdit = false

onMounted(async () => {
  if (!containerEl.value) return
  handle = await mountQuoteGrid(containerEl.value, props.columns, props.modelValue)
  handle.applyDoubtMarks(props.doubtMarks ?? [])
  handle.onRowsChanged((rows) => {
    emittingFromEdit = true
    emit('update:modelValue', rows)
    emittingFromEdit = false
  })
})

watch(() => props.modelValue, (rows) => {
  if (emittingFromEdit || !handle) return
  handle.loadRows(rows)
  handle.applyDoubtMarks(props.doubtMarks ?? [])
})

watch(() => props.doubtMarks, (marks) => {
  handle?.applyDoubtMarks(marks ?? [])
})

onBeforeUnmount(() => {
  handle?.dispose()
  handle = null
})
</script>

<template>
  <div ref="containerEl" class="quote-grid" style="height:520px;border:1px solid #e8e8e8;border-radius:4px"></div>
</template>
