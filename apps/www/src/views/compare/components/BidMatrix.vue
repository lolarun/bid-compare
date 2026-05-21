<script setup lang="ts">
import { computed, ref } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { normalizeAlert, alertColors, formatDeviation } from '@/utils/alert'
import type { MatrixRow, MatrixTotal } from '@/api/client'

interface SupplierInfo {
  id: number
  letter: string
  name: string
}

const props = defineProps<{
  suppliers: SupplierInfo[]
  rows: MatrixRow[]
  totals: MatrixTotal[]
  loading?: boolean
}>()

const totalsBySupplier = computed(() => {
  const map = new Map<number, MatrixTotal>()
  for (const t of props.totals) map.set(t.supplier_id, t)
  return map
})

/* ---------- virtual scroll ---------- */
const scrollRef = ref<HTMLElement | null>(null)
const ROW_HEIGHT = 68 // estimated average row height

const virtualizer = useVirtualizer(
  computed(() => ({
    count: props.rows.length,
    getScrollElement: () => scrollRef.value,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10, // render 10 extra rows above/below viewport for smooth scrolling
  })),
)

const virtualRows = computed(() => virtualizer.value.getVirtualItems())
const totalHeight = computed(() => virtualizer.value.getTotalSize())

const paddingTop = computed(() =>
  virtualRows.value.length > 0 ? virtualRows.value[0].start : 0,
)
const paddingBottom = computed(() =>
  virtualRows.value.length > 0
    ? totalHeight.value - virtualRows.value[virtualRows.value.length - 1].end
    : 0,
)

/* ---------- row count summary ---------- */
const rowCountText = computed(() => {
  const n = props.rows.length
  return n > 0 ? `共 ${n} 条材料` : ''
})
</script>

<template>
  <a-spin :spinning="!!loading">
    <div class="bid-matrix__toolbar" v-if="rows.length > 0">
      <span class="bid-matrix__count">{{ rowCountText }}</span>
    </div>
    <div ref="scrollRef" class="bid-matrix">
      <table class="bid-matrix__table">
        <thead>
          <tr>
            <th class="bid-matrix__col-material">材料</th>
            <th class="bid-matrix__col-history">历史均价</th>
            <th class="bid-matrix__col-low">合理史低</th>
            <th v-for="s in suppliers" :key="s.id" class="bid-matrix__col-supplier">
              <div class="bid-matrix__supplier-tag">{{ s.letter }}</div>
              <div class="bid-matrix__supplier-name">{{ s.name }}</div>
            </th>
            <th class="bid-matrix__col-min">最低偏差</th>
            <th class="bid-matrix__col-rec">推荐</th>
          </tr>
        </thead>
        <tbody>
          <!-- top spacer row to maintain scroll position -->
          <tr v-if="paddingTop > 0" :style="{ height: paddingTop + 'px' }" aria-hidden="true">
            <td :colspan="3 + suppliers.length + 2" style="padding:0;border:none" />
          </tr>

          <tr
            v-for="vRow in virtualRows"
            :key="rows[vRow.index].material_id"
            :data-index="vRow.index"
            :ref="(el) => virtualizer.measureElement(el as HTMLElement)"
          >
            <td class="bid-matrix__cell-material">
              <div style="font-weight:500">{{ rows[vRow.index].material_name }}</div>
              <div style="font-size:12px;color:rgba(0,0,0,0.45)">{{ rows[vRow.index].spec }}</div>
            </td>
            <td>
              <div v-if="rows[vRow.index].historical_avg">
                <div style="font-weight:500">¥{{ rows[vRow.index].historical_avg!.price.toFixed(2) }}</div>
                <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                  {{ rows[vRow.index].historical_avg!.period }} · {{ rows[vRow.index].historical_avg!.projects }} 项目
                </div>
              </div>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td>
              <div v-if="rows[vRow.index].reasonable_low">
                <div style="font-weight:600;color:#52c41a">¥{{ rows[vRow.index].reasonable_low!.price.toFixed(2) }}</div>
                <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                  {{ rows[vRow.index].reasonable_low!.date }} · {{ rows[vRow.index].reasonable_low!.project }}
                </div>
              </div>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td
              v-for="cell in rows[vRow.index].suppliers"
              :key="cell.supplier_id"
              :class="{
                'bid-matrix__cell-lowest': cell.is_lowest,
                'bid-matrix__cell-empty': cell.price === null,
              }"
            >
              <template v-if="cell.price !== null">
                <div class="flex flex-between" style="align-items:flex-start">
                  <div style="font-weight:500">¥{{ cell.price.toFixed(2) }}</div>
                  <a-tag v-if="cell.is_lowest" color="green" style="margin:0;line-height:18px;font-size:10px">最低</a-tag>
                </div>
                <div :style="{ fontSize: '11px', color: alertColors[normalizeAlert(cell.alert_level)] }">
                  {{ formatDeviation(cell.deviation_pct) }}
                </div>
                <div v-if="cell.total !== null" style="font-size:11px;color:rgba(0,0,0,0.45)">
                  合计 ¥{{ cell.total.toLocaleString() }}
                </div>
              </template>
              <span v-else style="color:rgba(0,0,0,0.35)">未报价</span>
            </td>
            <td>
              <span
                v-if="rows[vRow.index].min_deviation !== null"
                :style="{ color: alertColors[normalizeAlert(rows[vRow.index].min_deviation! <= 0.05 ? 'normal' : rows[vRow.index].min_deviation! <= 0.1 ? 'yellow' : 'red')] }"
              >
                {{ formatDeviation(rows[vRow.index].min_deviation) }}
              </span>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td>
              <a-tag v-if="rows[vRow.index].recommended" color="blue">{{ rows[vRow.index].recommended }}</a-tag>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
          </tr>

          <!-- bottom spacer row to maintain scroll position -->
          <tr v-if="paddingBottom > 0" :style="{ height: paddingBottom + 'px' }" aria-hidden="true">
            <td :colspan="3 + suppliers.length + 2" style="padding:0;border:none" />
          </tr>
        </tbody>
        <tfoot>
          <tr>
            <td colspan="3" class="bid-matrix__footer-label">汇总（已报材料合计）</td>
            <td v-for="s in suppliers" :key="s.id">
              <div style="font-weight:600">
                ¥{{ totalsBySupplier.get(s.id)?.total.toLocaleString() ?? '—' }}
              </div>
              <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                平均偏差
                <span :style="{
                  color: alertColors[normalizeAlert(
                    Math.abs(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) <= 0.05
                      ? 'normal'
                      : Math.abs(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) <= 0.1
                      ? 'yellow'
                      : 'red'
                  )]
                }">
                  {{ formatDeviation(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) }}
                </span>
              </div>
            </td>
            <td colspan="2"></td>
          </tr>
        </tfoot>
      </table>
    </div>
  </a-spin>
</template>

<style scoped lang="less">
@import '@/styles/variables.less';

.bid-matrix {
  overflow-x: auto;
  overflow-y: auto;
  height: 70vh;
  contain: layout paint;
  will-change: transform;

  &__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: #fafafa;
    border: 1px solid @border-color-split;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    font-size: 13px;
    color: @text-color-secondary;
  }

  &__count {
    font-weight: 500;
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;

    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid @border-color-split;
      vertical-align: top;
      text-align: left;
    }

    th {
      background: #fafafa;
      color: @text-color-secondary;
      font-weight: 600;
      font-size: 12px;
      white-space: nowrap;
      position: sticky;
      top: 0;
      z-index: 2;
    }
  }

  &__col-material { min-width: 160px; }
  &__col-history,
  &__col-low { min-width: 130px; }
  &__col-supplier { min-width: 130px; }
  &__col-min,
  &__col-rec { min-width: 80px; }

  &__supplier-tag {
    display: inline-block;
    background: @primary-color;
    color: #fff;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    font-size: 11px;
    text-align: center;
    line-height: 18px;
    margin-right: 4px;
  }

  &__supplier-name {
    display: inline-block;
    font-size: 12px;
    color: @text-color;
  }

  &__cell-material {
    position: sticky;
    left: 0;
    background: #fff;
    z-index: 1;
  }

  &__cell-lowest {
    background: rgba(82, 196, 26, 0.06);
  }
  &__cell-empty {
    background: #fafafa;
    color: rgba(0, 0, 0, 0.35);
  }

  &__footer-label {
    text-align: right;
    font-weight: 600;
    background: #fafafa;
    color: @text-color;
  }

  tfoot td {
    background: #fafafa;
    position: sticky;
    bottom: 0;
    z-index: 2;
  }
}
</style>
