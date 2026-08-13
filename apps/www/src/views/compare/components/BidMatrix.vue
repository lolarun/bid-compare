<script setup lang="ts">
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import { DownloadOutlined } from '@ant-design/icons-vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { normalizeAlert, formatDeviation } from '@/utils/alert'
import { exportApi } from '@/api'
import type { MatrixRow, MatrixTotal, SupplierCell } from '@/api/client'

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
  category?: string
  projectId?: number
  supplierIds?: number[]
  anchorMatrix?: boolean          // v2.5: true when anchor-full-axis mode
  pendingItemLoading?: Record<number, boolean>
}>()

const emit = defineEmits<{
  (e: 'confirmItem', itemId: number, action: 'align' | 'exclude'): void
}>()

// B3 兼容期收尾（design/22 §B3）：列身份 join 原先按 t.supplier_id === s.id，
// 现改用 submission_id（legacy 模式下为 null，退回通用列身份键 id——此时
// 二者同值）。
const totalsBySupplier = computed(() => {
  const map = new Map<number, MatrixTotal>()
  for (const t of props.totals) map.set(t.submission_id ?? t.id, t)
  return map
})

// Completeness: only quoted/aggregated cells count (pending cells excluded)
const completeness = computed(() => {
  const total = props.rows.length
  const map = new Map<number, { quoted: number; total: number }>()
  for (const s of props.suppliers) {
    map.set(s.id, { quoted: 0, total })
  }
  for (const row of props.rows) {
    for (const cell of row.suppliers) {
      const status = cell.cell_status
      const isConfirmed = !status || status === 'quoted' || status === 'aggregated'
      if (cell.price !== null && isConfirmed) {
        const entry = map.get(cell.submission_id ?? cell.id)
        if (entry) entry.quoted++
      }
    }
  }
  return map
})

// Summary: pending cell count across entire matrix
const pendingCellCount = computed(() =>
  props.rows.reduce(
    (n, row) => n + row.suppliers.filter((c) => c.cell_status === 'pending').length,
    0,
  ),
)


/* ---------- Cell helpers ---------- */
function cellClass(cell: SupplierCell): Record<string, boolean> {
  const status = cell.cell_status
  return {
    'bid-matrix__cell-lowest': cell.is_lowest,
    'bid-matrix__cell-empty': !status ? cell.price === null : status === 'missing' || status === 'excluded',
    'bid-matrix__cell-pending': status === 'pending',
    'bid-matrix__cell-excluded': status === 'excluded',
  }
}

function isPendingLoading(itemId: number | null | undefined): boolean {
  if (!itemId || !props.pendingItemLoading) return false
  return !!props.pendingItemLoading[itemId]
}

/* ---------- virtual scroll ---------- */
const scrollRef = ref<HTMLElement | null>(null)
const ROW_HEIGHT = 80  // slightly taller to accommodate cell_status badges

const virtualizer = useVirtualizer(
  computed(() => ({
    count: props.rows.length,
    getScrollElement: () => scrollRef.value,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
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

const rowCountText = computed(() => {
  const n = props.rows.length
  if (n === 0) return ''
  return props.anchorMatrix
    ? `采购清单 ${n} 项（全量主轴）`
    : `共 ${n} 条材料`
})

/* ---------- export ---------- */
const exporting = ref(false)
async function handleExport() {
  if (!props.supplierIds?.length) {
    message.warning('无供应商数据可导出')
    return
  }
  exporting.value = true
  try {
    const { data } = await exportApi.bidMatrix({
      supplier_ids: props.supplierIds.join(','),
      project_id: props.projectId,
      category: props.category,
    })
    const url = URL.createObjectURL(data)
    const a = document.createElement('a')
    a.href = url
    a.download = `比价矩阵_${props.category || ''}_${new Date().toISOString().slice(0, 10)}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
    message.success('导出成功')
  } catch {
    message.error('导出失败')
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <a-spin :spinning="!!loading">
    <!-- Toolbar -->
    <div class="bid-matrix__toolbar" v-if="rows.length > 0">
      <div>
        <span class="bid-matrix__count">{{ rowCountText }}</span>
        <span v-if="anchorMatrix && pendingCellCount > 0" class="bid-matrix__pending-hint">
          ⚠ {{ pendingCellCount }} 个格子待确认（橙色标注，未计入最低价）
        </span>
        <span v-else class="bid-matrix__legend">
          绿色为该项最低价，灰底为未报价，偏差对比历史均价
        </span>
      </div>
      <a-space>
        <a-button size="small" :loading="exporting" @click="handleExport">
          <template #icon><DownloadOutlined /></template>
          导出矩阵
        </a-button>
      </a-space>
    </div>

    <!-- Matrix table -->
    <div ref="scrollRef" class="bid-matrix">
      <table class="bid-matrix__table">
        <thead>
          <tr>
            <th class="bid-matrix__col-material" rowspan="2">
              材料
              <span v-if="anchorMatrix" style="font-size:10px;color:#1890ff;margin-left:4px;font-weight:400">锚点全量</span>
            </th>
            <th class="bid-matrix__col-history" rowspan="2">历史均价</th>
            <th
              v-for="s in suppliers"
              :key="s.id"
              class="bid-matrix__col-supplier-header"
              colspan="1"
            >
              <span class="bid-matrix__supplier-tag">{{ s.letter }}</span>
              <span class="bid-matrix__supplier-name">{{ s.name }}</span>
            </th>
            <th class="bid-matrix__col-min" rowspan="2">最低偏差</th>
            <th class="bid-matrix__col-rec" rowspan="2">推荐</th>
          </tr>
        </thead>
        <tbody>
          <!-- top spacer -->
          <tr v-if="paddingTop > 0" :style="{ height: paddingTop + 'px' }" aria-hidden="true">
            <td :colspan="2 + suppliers.length + 2" style="padding:0;border:none" />
          </tr>

          <tr
            v-for="vRow in virtualRows"
            :key="rows[vRow.index].anchor_seq ?? rows[vRow.index].material_id ?? vRow.index"
            :data-index="vRow.index"
            :ref="(el) => virtualizer.measureElement(el as HTMLElement)"
          >
            <!-- Material / anchor name -->
            <td class="bid-matrix__cell-material">
              <div style="font-weight:500">{{ rows[vRow.index].material_name }}</div>
              <div style="font-size:12px;color:rgba(0,0,0,0.45)">{{ rows[vRow.index].spec }}</div>
              <div v-if="rows[vRow.index].anchor_seq" style="font-size:10px;color:rgba(0,0,0,0.3);margin-top:1px">
                #{{ rows[vRow.index].anchor_seq }}
              </div>
            </td>

            <!-- Historical avg -->
            <td class="bid-matrix__cell-history">
              <template v-if="rows[vRow.index].historical_avg">
                <div class="bid-matrix__hist-price">¥{{ rows[vRow.index].historical_avg!.price.toFixed(2) }}</div>
                <div style="font-size:11px;color:rgba(0,0,0,0.45)">
                  {{ rows[vRow.index].historical_avg!.period }}
                </div>
              </template>
              <span v-else style="color:rgba(0,0,0,0.35)">—</span>
            </td>

            <!-- Supplier cells -->
            <td
              v-for="cell in rows[vRow.index].suppliers"
              :key="cell.submission_id ?? cell.id"
              :class="cellClass(cell)"
            >
              <!-- quoted / aggregated / legacy (no cell_status) -->
              <template v-if="!cell.cell_status || cell.cell_status === 'quoted' || cell.cell_status === 'aggregated'">
                <template v-if="cell.price !== null">
                  <div class="bid-matrix__price-row">
                    <span class="bid-matrix__price">¥{{ cell.price.toFixed(2) }}</span>
                    <span v-if="cell.is_lowest" class="bid-matrix__lowest-badge">★ 最低</span>
                    <span v-if="cell.cell_status === 'aggregated'" class="bid-matrix__agg-badge">聚合</span>
                  </div>
                  <span
                    class="bid-matrix__deviation-pill"
                    :class="`bid-matrix__deviation-pill--${normalizeAlert(cell.alert_level)}`"
                  >
                    {{ formatDeviation(cell.deviation_pct) }}
                  </span>
                </template>
                <span v-else class="bid-matrix__no-quote">未报价</span>
              </template>

              <!-- pending: show reference price in orange, provide inline action buttons -->
              <template v-else-if="cell.cell_status === 'pending'">
                <div class="bid-matrix__pending-cell">
                  <div class="bid-matrix__price-row">
                    <a-tooltip v-if="cell.evidence" :title="cell.evidence">
                      <span class="bid-matrix__price bid-matrix__price--pending" style="cursor:help">
                        {{ cell.price != null ? `¥${cell.price.toFixed(2)}` : '—' }}
                      </span>
                    </a-tooltip>
                    <span v-else class="bid-matrix__price bid-matrix__price--pending">
                      {{ cell.price != null ? `¥${cell.price.toFixed(2)}` : '—' }}
                    </span>
                    <span class="bid-matrix__pending-badge">待确认</span>
                  </div>
                  <!-- Inline confirm buttons (only if parent listens to confirmItem) -->
                  <div v-if="cell.item_id" class="bid-matrix__pending-actions">
                    <a-button
                      type="primary"
                      size="small"
                      style="font-size:10px;height:20px;padding:0 6px"
                      :loading="isPendingLoading(cell.item_id)"
                      @click.stop="emit('confirmItem', cell.item_id!, 'align')"
                    >纳入</a-button>
                    <a-button
                      danger size="small"
                      style="font-size:10px;height:20px;padding:0 6px"
                      :loading="isPendingLoading(cell.item_id)"
                      @click.stop="emit('confirmItem', cell.item_id!, 'exclude')"
                    >排除</a-button>
                  </div>
                </div>
              </template>

              <!-- excluded -->
              <template v-else-if="cell.cell_status === 'excluded'">
                <span class="bid-matrix__excluded">已排除</span>
              </template>

              <!-- missing -->
              <template v-else>
                <span class="bid-matrix__no-quote">未报价</span>
              </template>
            </td>

            <!-- Min deviation -->
            <td>
              <span
                v-if="rows[vRow.index].min_deviation !== null"
                class="bid-matrix__deviation-pill"
                :class="`bid-matrix__deviation-pill--${normalizeAlert(
                  rows[vRow.index].min_deviation! <= 0.05 ? 'normal' : rows[vRow.index].min_deviation! <= 0.1 ? 'yellow' : 'red'
                )}`"
              >
                {{ formatDeviation(rows[vRow.index].min_deviation) }}
              </span>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>

            <!-- Recommended -->
            <td>
              <a-tag v-if="rows[vRow.index].recommended" color="blue" style="margin:0">{{ rows[vRow.index].recommended }}</a-tag>
              <span v-else style="color:rgba(0,0,0,0.45)">—</span>
            </td>
          </tr>

          <!-- bottom spacer -->
          <tr v-if="paddingBottom > 0" :style="{ height: paddingBottom + 'px' }" aria-hidden="true">
            <td :colspan="2 + suppliers.length + 2" style="padding:0;border:none" />
          </tr>
        </tbody>

        <!-- Footer: 3 rows -->
        <tfoot>
          <!-- Row 1: Totals (quoted-only) -->
          <tr>
            <td colspan="2" class="bid-matrix__footer-label">合计（已确认报价）</td>
            <td v-for="s in suppliers" :key="'total-' + s.id">
              <div style="font-weight:600;font-size:14px">
                ¥{{ totalsBySupplier.get(s.id)?.total?.toLocaleString() ?? '—' }}
              </div>
            </td>
            <td colspan="2"></td>
          </tr>
          <!-- Row 2: Avg deviation -->
          <tr>
            <td colspan="2" class="bid-matrix__footer-label">平均偏差</td>
            <td v-for="s in suppliers" :key="'dev-' + s.id">
              <span
                class="bid-matrix__deviation-pill"
                :class="`bid-matrix__deviation-pill--${normalizeAlert(
                  Math.abs(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) <= 0.05
                    ? 'normal'
                    : Math.abs(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) <= 0.1
                    ? 'yellow'
                    : 'red'
                )}`"
              >
                {{ formatDeviation(totalsBySupplier.get(s.id)?.avg_deviation ?? 0) }}
              </span>
            </td>
            <td colspan="2"></td>
          </tr>
          <!-- Row 3: Completeness (quoted/aggregated only) -->
          <tr>
            <td colspan="2" class="bid-matrix__footer-label">已确认报价完整度</td>
            <td v-for="s in suppliers" :key="'comp-' + s.id">
              <span :style="{ color: completeness.get(s.id)?.quoted === completeness.get(s.id)?.total ? '#52c41a' : 'rgba(0,0,0,0.65)' }">
                {{ completeness.get(s.id)?.quoted ?? 0 }}/{{ completeness.get(s.id)?.total ?? 0 }}
                <span v-if="completeness.get(s.id)?.quoted === completeness.get(s.id)?.total"> ✓</span>
              </span>
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
  height: 60vh;
  contain: layout paint;
  will-change: transform;

  &__toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 14px;
    background: #fafafa;
    border: 1px solid @border-color-split;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
  }

  &__count {
    font-size: 13px;
    font-weight: 500;
    color: @text-color;
    margin-right: 12px;
  }

  &__legend {
    font-size: 12px;
    color: @text-color-tertiary;
  }

  &__pending-hint {
    font-size: 12px;
    color: #faad14;
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
  &__col-history { min-width: 110px; }
  &__col-supplier-header { min-width: 148px; text-align: center; }
  &__col-min,
  &__col-rec { min-width: 80px; }

  &__supplier-tag {
    display: inline-block;
    background: @primary-color;
    color: #fff;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    font-size: 11px;
    text-align: center;
    line-height: 20px;
    margin-right: 6px;
    font-weight: 600;
  }

  &__supplier-name {
    font-size: 12px;
    color: @text-color;
  }

  &__cell-material {
    position: sticky;
    left: 0;
    background: #fff;
    z-index: 1;
  }

  &__cell-history {
    // slight emphasis
  }

  &__hist-price {
    font-weight: 500;
    color: @primary-color;
  }

  &__cell-lowest {
    background: rgba(82, 196, 26, 0.06);
  }

  &__cell-empty {
    background: #fafafa;
  }

  &__cell-pending {
    background: rgba(250, 173, 20, 0.04);
    border-left: 2px solid #faad14 !important;
  }

  &__cell-excluded {
    background: rgba(0, 0, 0, 0.02);
    opacity: 0.6;
  }

  &__price-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  &__price {
    font-weight: 500;

    &--pending {
      color: #faad14;
    }
  }

  &__lowest-badge {
    display: inline-block;
    background: #f6ffed;
    color: #52c41a;
    border: 1px solid #b7eb8f;
    border-radius: 4px;
    font-size: 10px;
    padding: 0 4px;
    line-height: 18px;
    white-space: nowrap;
    font-weight: 500;
  }

  &__agg-badge {
    display: inline-block;
    background: #e6f7ff;
    color: #1890ff;
    border: 1px solid #91d5ff;
    border-radius: 4px;
    font-size: 10px;
    padding: 0 4px;
    line-height: 18px;
    white-space: nowrap;
  }

  &__pending-badge {
    display: inline-block;
    background: #fffbe6;
    color: #faad14;
    border: 1px solid #ffe58f;
    border-radius: 4px;
    font-size: 10px;
    padding: 0 4px;
    line-height: 18px;
    white-space: nowrap;
    font-weight: 500;
  }

  &__pending-cell {
    // container
  }

  &__pending-actions {
    display: flex;
    gap: 4px;
    margin-top: 4px;
  }

  &__deviation-pill {
    display: inline-block;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 10px;
    line-height: 18px;
    font-weight: 500;

    &--normal {
      color: #52c41a;
      background: rgba(82, 196, 26, 0.08);
    }
    &--yellow {
      color: #faad14;
      background: rgba(250, 173, 20, 0.08);
    }
    &--red {
      color: #ff4d4f;
      background: rgba(255, 77, 79, 0.08);
    }
  }

  &__no-quote {
    color: rgba(0, 0, 0, 0.25);
    font-size: 12px;
  }

  &__excluded {
    color: rgba(0, 0, 0, 0.3);
    font-size: 12px;
    text-decoration: line-through;
  }

  &__footer-label {
    text-align: right;
    font-weight: 600;
    background: #fafafa;
    color: @text-color;
    font-size: 13px;
  }

  tfoot td {
    background: #fafafa;
    position: sticky;
    bottom: 0;
    z-index: 2;
    border-top: 1px solid @border-color-base;
  }
}
</style>
