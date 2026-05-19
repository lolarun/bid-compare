<script setup lang="ts">
import { computed } from 'vue'
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
</script>

<template>
  <a-spin :spinning="!!loading">
    <div class="bid-matrix">
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
          <tr v-for="row in rows" :key="row.material_id">
            <td class="bid-matrix__cell-material">
              <div style="font-weight:500">{{ row.material_name }}</div>
              <div style="font-size:12px;color:rgba(0,0,0,0.45)">{{ row.spec }}</div>
            </td>
            <td>
              <div v-if="row.historical_avg">
                <div style="font-weight:500">¥{{ row.historical_avg.price.toFixed(2) }}</div>
                <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                  {{ row.historical_avg.period }} · {{ row.historical_avg.projects }} 项目
                </div>
              </div>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td>
              <div v-if="row.reasonable_low">
                <div style="font-weight:600;color:#52c41a">¥{{ row.reasonable_low.price.toFixed(2) }}</div>
                <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                  {{ row.reasonable_low.date }} · {{ row.reasonable_low.project }}
                </div>
              </div>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td
              v-for="cell in row.suppliers"
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
                v-if="row.min_deviation !== null"
                :style="{ color: alertColors[normalizeAlert(row.min_deviation <= 0.05 ? 'normal' : row.min_deviation <= 0.1 ? 'yellow' : 'red')] }"
              >
                {{ formatDeviation(row.min_deviation) }}
              </span>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
            <td>
              <a-tag v-if="row.recommended" color="blue">{{ row.recommended }}</a-tag>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
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
      z-index: 1;
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

  tfoot td { background: #fafafa; }
}
</style>
