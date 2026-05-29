<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, ref, watch, computed, nextTick } from 'vue'
import echarts, { type EChartsType } from '@/utils/echarts'

// 项目 → 品类 → 金额
type Node = { name: string; value?: number; children?: Node[] }

const props = defineProps<{
  data?: Node[]
  loading?: boolean
}>()

const chartEl = ref<HTMLElement | null>(null)
const chart = shallowRef<EChartsType | null>(null)

const DEFAULT: Node[] = [
  {
    name: '项目 X 给排水材料采购',
    children: [
      { name: '钢管', value: 86000 },
      { name: 'PPR 管', value: 35000 },
      { name: '不锈钢水箱', value: 17000 },
      { name: '阀门', value: 22000 },
    ],
  },
  {
    name: '项目 Y 数据中心',
    children: [
      { name: '桥架', value: 160000 },
      { name: '电缆', value: 90000 },
      { name: '配电箱', value: 64000 },
      { name: '防火桥架', value: 38000 },
    ],
  },
  {
    name: '项目 Z 暖通改造',
    children: [
      { name: '风机盘管', value: 56000 },
      { name: '风口风阀', value: 28000 },
      { name: '保温管', value: 19000 },
    ],
  },
]

const isEmpty = computed(() => props.data !== undefined && props.data.length === 0)

function makeOption() {
  const data = props.data && props.data.length ? props.data : DEFAULT
  return {
    tooltip: {
      formatter: (info: { name: string; value: number }) =>
        `${info.name}<br/>金额：¥${info.value?.toLocaleString?.() ?? info.value}`,
    },
    series: [
      {
        type: 'treemap',
        roam: false,
        nodeClick: false,
        breadcrumb: { show: true, bottom: 4, itemStyle: { color: '#fff', textStyle: { color: '#666' } } },
        label: {
          show: true,
          formatter: (p: { name: string; value: number }) => {
            const v = p.value
            if (v >= 10000) return `${p.name}\n¥${(v / 10000).toFixed(1)}万`
            return `${p.name}\n¥${v.toLocaleString()}`
          },
          fontSize: 12,
          color: '#fff',
        },
        upperLabel: {
          show: true,
          height: 20,
          color: '#fff',
          fontSize: 12,
        },
        levels: [
          {
            itemStyle: { borderColor: '#fff', borderWidth: 4, gapWidth: 4 },
          },
          {
            itemStyle: { borderColor: '#fff', borderWidth: 2, gapWidth: 2 },
            colorSaturation: [0.3, 0.6],
          },
        ],
        visualDimension: 0,
        visualMin: 0,
        colorMappingBy: 'value',
        color: ['#1677ff', '#69c0ff', '#bae0ff', '#ffe7ba', '#ffa940', '#ff4d4f'],
        data,
      },
    ],
  }
}

function ensure() {
  if (!chartEl.value) return
  if (!chart.value) chart.value = echarts.init(chartEl.value)
  chart.value.setOption(makeOption(), true)
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
    <div v-if="isEmpty" class="tree-heatmap tree-heatmap--empty">
      <a-empty description="暂无热力图数据" />
    </div>
    <div v-show="!isEmpty" ref="chartEl" class="tree-heatmap"></div>
  </a-spin>
</template>

<style scoped>
.tree-heatmap { width: 100%; height: 360px; }
.tree-heatmap--empty { display: flex; align-items: center; justify-content: center; }
</style>
