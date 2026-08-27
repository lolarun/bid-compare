<!--
  design/44 §5 —— 轮次趋势：消费 GET /api/analysis/round-trend，不重算任何
  矩阵语义（后端 round_trend.py 已经算好了 comparable/discount，这里只负责
  摆出来）。只在 ≥2 轮时才有意义，调用方（WorkspaceView）据此决定要不要
  显示这个 tab。

  R3/R4（design/42 §5）在这里的体现：not_comparable_reason 非空时**不显示
  折扣数字**，显示原因文案；缺席的供应商那一轮直接没有行，不是显示 0。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { analysisApi } from '@/api'
import type { RoundTrendResult, RoundTrendSupplier } from '@/api/client'

const props = defineProps<{
  projectId: number
  category: string
}>()

const loading = ref(false)
const result = ref<RoundTrendResult | null>(null)
const error = ref('')

async function load() {
  if (!props.projectId || !props.category) return
  loading.value = true
  error.value = ''
  try {
    const { data } = await analysisApi.roundTrend({ project_id: props.projectId, category: props.category })
    result.value = data
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '轮次趋势加载失败'
    result.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => [props.projectId, props.category], load)

// 供应商 → 该供应商每一轮的汇总，按轮次序号排。
const supplierRows = computed(() => {
  const data = result.value
  if (!data) return []
  const byName = new Map<string, RoundTrendSupplier[]>()
  for (const s of data.suppliers) {
    const key = s.supplier_name || `supplier-${s.supplier_id}`
    if (!byName.has(key)) byName.set(key, [])
    byName.get(key)!.push(s)
  }
  return Array.from(byName.entries()).map(([name, rows]) => ({
    name,
    rows: rows.sort((a, b) => a.round_seq - b.round_seq),
  }))
})

function fmtMoney(v: number | null) {
  if (v == null) return '—'
  return `¥${v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
function fmtPct(v: number | null) {
  if (v == null) return null
  const sign = v > 0 ? '↓' : v < 0 ? '↑' : ''
  return `${sign}${Math.abs(v).toFixed(1)}%`
}
</script>

<template>
  <div class="round-trend-panel">
    <a-spin :spinning="loading">
      <a-alert v-if="error" type="error" :message="error" show-icon style="margin-bottom:12px" />

      <template v-if="result">
        <a-alert
          v-for="skip in result.skipped_rounds" :key="skip.round_id"
          type="warning" show-icon style="margin-bottom:8px"
          :message="`第${skip.seq}轮未计入趋势：${skip.reason}`"
        />

        <a-empty v-if="supplierRows.length === 0 && !loading" description="暂无可对比的跨轮次数据" />

        <div v-for="sup in supplierRows" :key="sup.name" class="round-trend-panel__supplier">
          <div class="round-trend-panel__supplier-name">{{ sup.name }}</div>
          <a-table
            :data-source="sup.rows"
            :columns="[
              { title: '轮次', dataIndex: 'round_seq', width: 80 },
              { title: '合计', dataIndex: 'total', width: 140 },
              { title: '环比', dataIndex: 'round_over_round_discount_pct', width: 160 },
              { title: '累计折扣', dataIndex: 'cumulative_discount_pct', width: 120 },
              { title: '排名', dataIndex: 'rank', width: 80 },
            ]"
            :pagination="false"
            size="small"
            row-key="round_id"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.dataIndex === 'round_seq'">第{{ record.round_seq }}轮</template>
              <template v-else-if="column.dataIndex === 'total'">{{ fmtMoney(record.total) }}</template>
              <template v-else-if="column.dataIndex === 'round_over_round_discount_pct'">
                <span v-if="!record.comparable_to_prev" class="round-trend-panel__na">
                  {{ record.not_comparable_reason || '本轮首次参与' }}
                </span>
                <span v-else :class="{
                  'round-trend-panel__down': (record.round_over_round_discount_pct ?? 0) > 0,
                  'round-trend-panel__up': (record.round_over_round_discount_pct ?? 0) < 0,
                }">
                  {{ fmtPct(record.round_over_round_discount_pct) ?? '持平' }}
                </span>
              </template>
              <template v-else-if="column.dataIndex === 'cumulative_discount_pct'">
                {{ record.cumulative_discount_pct != null ? fmtPct(record.cumulative_discount_pct) : '—' }}
              </template>
              <template v-else-if="column.dataIndex === 'rank'">
                <a-tag v-if="record.rank === 1" color="gold">#1</a-tag>
                <span v-else-if="record.rank">#{{ record.rank }}</span>
                <span v-else>—</span>
              </template>
            </template>
          </a-table>
        </div>
      </template>
    </a-spin>
  </div>
</template>

<style scoped>
.round-trend-panel__supplier { margin-bottom: 20px; }
.round-trend-panel__supplier-name { font-weight: 600; margin-bottom: 8px; }
.round-trend-panel__down { color: #389e0d; }
.round-trend-panel__up { color: #cf1322; }
.round-trend-panel__na { color: rgba(0,0,0,0.45); font-size: 12px; }
</style>
