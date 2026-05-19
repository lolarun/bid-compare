<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, ref, watch } from 'vue'
import echarts, { type EChartsType } from '@/utils/echarts'

// 品类气泡：x = 索引，y = 专业组，大小 = 采购金额，颜色 = 专业
interface BubbleItem {
  name: string
  profession: '电气' | '给排水' | '暖通'
  amount: number
}

const props = defineProps<{
  data?: BubbleItem[]
  loading?: boolean
}>()

const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<EChartsType | null>(null)

const DEFAULT: BubbleItem[] = [
  { name: '桥架', profession: '电气', amount: 280000 },
  { name: '电缆', profession: '电气', amount: 320000 },
  { name: '配电箱', profession: '电气', amount: 160000 },
  { name: '防火桥架', profession: '电气', amount: 88000 },
  { name: '钢管', profession: '给排水', amount: 240000 },
  { name: 'PPR 管', profession: '给排水', amount: 92000 },
  { name: '不锈钢水箱', profession: '给排水', amount: 76000 },
  { name: '阀门', profession: '给排水', amount: 110000 },
  { name: '风机盘管', profession: '暖通', amount: 140000 },
  { name: '风口风阀', profession: '暖通', amount: 64000 },
  { name: '保温管', profession: '暖通', amount: 42000 },
]

const COLOR: Record<string, string> = {
  电气: '#1677ff',
  给排水: '#52c41a',
  暖通: '#fa8c16',
}

function makeOption() {
  const items = props.data && props.data.length ? props.data : DEFAULT
  const professions = ['电气', '给排水', '暖通']
  const grouped = professions.map((p) => items.filter((d) => d.profession === p))
  const maxAmt = Math.max(...items.map((d) => d.amount), 1)

  return {
    tooltip: {
      formatter: (p: { data: BubbleItem & { value: number[] } }) =>
        `${p.data.name}<br/>专业：${p.data.profession}<br/>金额：¥${p.data.amount.toLocaleString()}`,
    },
    legend: { bottom: 4, icon: 'circle', textStyle: { fontSize: 12 } },
    grid: { top: 20, left: 80, right: 24, bottom: 40 },
    xAxis: {
      type: 'value',
      show: false,
    },
    yAxis: {
      type: 'category',
      data: professions,
      axisLine: { lineStyle: { color: '#e5e5e0' } },
      axisLabel: { color: 'rgba(0,0,0,0.65)', fontSize: 13 },
    },
    series: professions.map((p, idx) => ({
      name: p,
      type: 'scatter',
      data: grouped[idx].map((d, i) => ({
        name: d.name,
        profession: d.profession,
        amount: d.amount,
        value: [i * 1.5 + 1, p],
      })),
      symbolSize: (_val: number[], d: { amount: number }) =>
        18 + (d.amount / maxAmt) * 60,
      itemStyle: { color: COLOR[p], opacity: 0.7 },
      label: {
        show: true,
        formatter: (p: { data: { name: string; amount: number } }) =>
          `${p.data.name}\n¥${(p.data.amount / 10000).toFixed(1)}万`,
        position: 'inside',
        color: '#fff',
        fontSize: 11,
      },
    })),
  }
}

function ensure() {
  if (!chartEl.value) return
  if (!chart.value) chart.value = echarts.init(chartEl.value)
  chart.value.setOption(makeOption(), false)
}
function onResize() { chart.value?.resize() }
onMounted(() => { ensure(); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart.value?.dispose()
  chart.value = null
})
watch(() => props.data?.length, ensure)
</script>

<template>
  <a-spin :spinning="!!loading">
    <div ref="chartEl" class="bubble-chart"></div>
  </a-spin>
</template>

<style scoped>
.bubble-chart { width: 100%; height: 360px; }
</style>
