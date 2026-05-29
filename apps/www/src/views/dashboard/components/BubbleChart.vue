<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, ref, watch, computed, nextTick } from 'vue'
import echarts, { type EChartsType } from '@/utils/echarts'

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

const COLOR: Record<string, string> = {
  电气: '#1677ff',
  给排水: '#52c41a',
  暖通: '#fa8c16',
}

const isEmpty = computed(() => props.data !== undefined && props.data.length === 0)

function makeOption(items: BubbleItem[]) {
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
    xAxis: { type: 'value' as const, show: false },
    yAxis: {
      type: 'category' as const,
      data: professions,
      axisLine: { lineStyle: { color: '#e5e5e0' } },
      axisLabel: { color: 'rgba(0,0,0,0.65)', fontSize: 13 },
    },
    series: professions.map((p, idx) => ({
      name: p,
      type: 'scatter' as const,
      data: grouped[idx].map((d, i) => ({
        name: d.name,
        profession: d.profession,
        amount: d.amount,
        value: [i * 1.5 + 1, p],
      })),
      symbolSize: (val: number[], row: { data: { amount: number } }) =>
        18 + ((row?.data?.amount ?? val[0]) / maxAmt) * 60,
      itemStyle: { color: COLOR[p], opacity: 0.7 },
      label: {
        show: true,
        formatter: (p: { data: { name: string; amount: number } }) =>
          `${p.data.name}\n¥${(p.data.amount / 10000).toFixed(1)}万`,
        position: 'inside' as const,
        color: '#fff',
        fontSize: 11,
      },
    })),
  }
}

function ensure() {
  if (!chartEl.value) return
  if (!chart.value) chart.value = echarts.init(chartEl.value)
  const items = props.data && props.data.length ? props.data : undefined
  if (items) {
    chart.value.setOption(makeOption(items), true)
  }
}

function onResize() { chart.value?.resize() }

onMounted(() => { ensure(); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart.value?.dispose()
  chart.value = null
})

watch(() => [props.data?.length], () => nextTick(ensure))
</script>

<template>
  <a-spin :spinning="!!loading">
    <div v-if="isEmpty" class="bubble-chart bubble-chart--empty">
      <a-empty description="暂无气泡图数据" />
    </div>
    <div v-show="!isEmpty" ref="chartEl" class="bubble-chart"></div>
  </a-spin>
</template>

<style scoped>
.bubble-chart { width: 100%; height: 360px; }
.bubble-chart--empty { display: flex; align-items: center; justify-content: center; }
</style>
