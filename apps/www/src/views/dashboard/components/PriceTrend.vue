<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, ref, watch } from 'vue'
import echarts, { type EChartsType } from '@/utils/echarts'

const props = defineProps<{
  series?: { name: string; data: number[] }[]
  xAxis?: string[]
  loading?: boolean
}>()

const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<EChartsType | null>(null)

const DEFAULT_X = ['11月', '12月', '1月', '2月', '3月', '4月']
const DEFAULT_SERIES = [
  { name: 'DN100 钢管', data: [68, 70, 71, 73, 75, 78] },
  { name: '配电箱', data: [1080, 1100, 1150, 1180, 1220, 1280] },
  { name: 'PPR 管', data: [12.5, 12.8, 12.6, 12.7, 12.9, 12.5] },
]

function makeOption() {
  const series = props.series && props.series.length ? props.series : DEFAULT_SERIES
  const xAxis = props.xAxis && props.xAxis.length ? props.xAxis : DEFAULT_X
  return {
    color: ['#1677ff', '#faad14', '#52c41a', '#722ed1'],
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line' },
    },
    legend: {
      bottom: 0,
      icon: 'circle',
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: 'rgba(0,0,0,0.65)', fontSize: 12 },
    },
    grid: { top: 20, left: 50, right: 16, bottom: 40 },
    xAxis: {
      type: 'category',
      data: xAxis,
      axisLine: { lineStyle: { color: '#e5e5e0' } },
      axisLabel: { color: 'rgba(0,0,0,0.55)', fontSize: 12 },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#f0f0f0', type: 'dashed' } },
      axisLabel: { color: 'rgba(0,0,0,0.55)', fontSize: 12 },
    },
    series: series.map((s) => ({
      name: s.name,
      type: 'line',
      data: s.data,
      smooth: true,
      symbol: 'circle',
      symbolSize: 6,
      lineStyle: { width: 2 },
    })),
  }
}

function ensureChart() {
  if (!chartEl.value) return
  if (!chart.value) {
    chart.value = echarts.init(chartEl.value)
  }
  chart.value.setOption(makeOption())
}

function handleResize() {
  chart.value?.resize()
}

onMounted(() => {
  ensureChart()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart.value?.dispose()
  chart.value = null
})

watch(() => [props.series?.length, props.xAxis?.length], () => ensureChart())
</script>

<template>
  <a-spin :spinning="!!loading">
    <div ref="chartEl" class="price-trend"></div>
  </a-spin>
</template>

<style scoped>
.price-trend {
  width: 100%;
  height: 280px;
}
</style>
