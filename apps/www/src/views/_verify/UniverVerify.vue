<!--
  design/27 §5/§10 步骤2 —— Univer 选型验证台，非产品页面，验证通过/不通过后删除。

  现在直接用 QuoteGrid.vue（生产候选组件）+ quoteGridController.ts（唯一
  接触 Univer 对象的模块），不再在这个页面里直接调 Univer API——这样本页面
  本身就是"业务代码只碰 QuoteGrid，不碰 Univer"这条隔离边界的第一个真实
  验证，不是另起一套验证代码。

  验证四条线（§5，提交集成前必须过）：
  1. Excel 剪贴板往返 —— 需要真实剪贴板/受信任事件，本沙盒环境验不了，
     交由用户在这个页面手动验证。
  2. 300+ 行流畅 —— 137 行真实宏胜数据 + 163 行合成填充。
  3. 单元格标色 API —— 红/黄/橙三种疑点色，程序化验证已通过（见对话记录）。
  4. 中文输入法输入干净 —— 同①，需要人工验证。
-->
<script setup lang="ts">
import { ref } from 'vue'
import QuoteGrid from '@/components/QuoteGrid.vue'
import type { DoubtMark, QuoteGridColumn } from '@/univer/quoteGridController'
import hongshengReal from './hongsheng_real.json'

const columns: QuoteGridColumn[] = [
  { key: 'material', title: '材料/设备名称' },
  { key: 'spec', title: '规格型号' },
  { key: 'unit', title: '计量单位' },
  { key: 'qty', title: '数量' },
  { key: 'unit_price', title: '单价' },
  { key: 'total_price', title: '合价' },
  { key: 'brand', title: '品牌' },
  { key: 'remark', title: '备注' },
]

const real = hongshengReal as Record<string, unknown>[]
const syntheticCount = 163
const synthetic: Record<string, unknown>[] = Array.from({ length: syntheticCount }, (_, i) => ({
  material: `填充测试行 ${i + 1}（合成数据，非真实宏胜数据）`,
  spec: `TEST-${i + 1}`, unit: '个', qty: 10 + i, unit_price: 100, total_price: (10 + i) * 100,
  brand: '', remark: '',
}))

const rows = ref<Record<string, unknown>[]>([...real, ...synthetic])

// 模拟三种疑点判据落在真实数据的前几行——跟生产里 validation_flags/qty 缺失
// 的实际来源不同（这里是手动挑的演示行），但标色/悬浮路径跟真实数据完全一致。
const doubtMarks: DoubtMark[] = [
  { row: 1, columnKey: 'qty', severity: 'missing', hoverText: '第2行没读到数量，请核对原文' },
  { row: 2, columnKey: 'total_price', severity: 'arithmetic', hoverText: '数量×单价与合价对不上，三者中至少一个可能读错' },
  { row: 3, columnKey: 'unit_price', severity: 'truncation', hoverText: '单价数值疑似被截断，建议核对原文' },
]

const editCount = ref(0)
function onModelUpdate(next: Record<string, unknown>[]) {
  rows.value = next
  editCount.value++
}
</script>

<template>
  <div style="padding:12px;font-family:sans-serif">
    <h3>Univer 选型验证台（design/27 §5，非产品页面）</h3>
    <div style="margin-bottom:8px;font-size:13px">
      共 {{ rows.length }} 行（真实宏胜 137 + 合成填充 163）· 编辑事件计数：{{ editCount }}
    </div>
    <QuoteGrid
      :model-value="rows"
      :columns="columns"
      :doubt-marks="doubtMarks"
      @update:model-value="onModelUpdate"
    />
    <div style="margin-top:10px;font-size:12px;color:#666">
      红=第2行数量（缺）· 黄=第3行合价（算术疑点）· 橙=第4行单价（截断疑点），悬浮可看批注说明。
    </div>
  </div>
</template>
