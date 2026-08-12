<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  CheckCircleOutlined, SearchOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { analysisApi } from '@/api'
import type { AnchorReviewMatrixResult, ReviewRow, ReviewCell, ReviewSupplier } from '@/api/client'

const props = defineProps<{
  projectId: number
  category: string
  submissionIds?: number[]  // §7 authoritative: BidSubmission IDs for this comparison
}>()

const emit = defineEmits<{
  (e: 'pending-count', count: number): void
}>()

// ─── Data ────────────────────────────────────────────────────────────────────
const result = ref<AnchorReviewMatrixResult | null>(null)
const loading = ref(false)
const confirmLoading = ref<Record<number, boolean>>({})
const expandedCells = ref<Record<string, boolean>>({})   // key: `${anchor_seq}_${supplier_id}`
// design/23：R1 止血时这里只写组件内 ref、从不调后端（评审点名的假按钮）——
// missing 单元格没有 BidAlignmentItem 可以挂状态，BidAlignmentItem 的 CHECK
// 约束又不允许"两个 FK 都空"表达"确认无报价"，当时判定需要设计讨论。现在
// design/23 落地：AnchorMissingAck 单开一张表持久化这个确认，不再是本地
// 状态——cell.missing_acked 直接来自后端 anchor-review/matrix 的响应。
const missingAckLoading = ref<Record<string, boolean>>({})

async function setMissingAck(anchorSeq: string, submissionId: number, acked: boolean) {
  const key = `${anchorSeq}_${submissionId}`
  if (missingAckLoading.value[key]) return
  missingAckLoading.value[key] = true
  try {
    await analysisApi.anchorReviewMissingAck({
      project_id: props.projectId,
      category: props.category,
      anchor_seq: anchorSeq,
      submission_id: submissionId,
      acked,
    })
    // 成功后直接改本地结果里的这一格，不用整张矩阵重新拉一遍。
    const row = result.value?.rows.find((r) => r.anchor_seq === anchorSeq)
    const cell = row?.cells[String(submissionId)]
    if (cell) cell.missing_acked = acked
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail ?? (acked ? '确认失败，请重试' : '取消确认失败，请重试'))
  } finally {
    missingAckLoading.value[key] = false
  }
}

async function load() {
  if (!props.projectId || !props.category) return
  loading.value = true
  try {
    const { data } = await analysisApi.anchorReviewMatrix({
      project_id: props.projectId,
      category: props.category,
      submission_ids: props.submissionIds?.length ? props.submissionIds.join(',') : undefined,
    })
    result.value = data
    emit('pending-count', data.pending_cells)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    message.error(detail ?? '加载复核矩阵失败')
  } finally {
    loading.value = false
  }
}

watch(() => [props.projectId, props.category, props.submissionIds], load, { immediate: true })

// ─── Filter ───────────────────────────────────────────────────────────────────
type FilterKey = 'needs_action' | 'all' | 'pending' | 'missing' | 'low'
const activeFilter = ref<FilterKey>('all')
const searchText = ref('')

function rowNeedsAction(row: ReviewRow): boolean {
  if (row.quoted_count < 2) return true
  const cells = Object.values(row.cells)
  return cells.some(c => c.cell_status === 'pending' || c.cell_status === 'missing')
}

const filteredRows = computed(() => {
  if (!result.value) return []
  let rows = result.value.rows

  // Text search
  const q = searchText.value.trim().toLowerCase()
  if (q) {
    rows = rows.filter(r =>
      r.anchor_name.toLowerCase().includes(q) ||
      r.anchor_spec.toLowerCase().includes(q) ||
      r.anchor_seq.toLowerCase().includes(q)
    )
  }

  // Status filter
  if (activeFilter.value === 'needs_action') {
    rows = rows.filter(rowNeedsAction)
  } else if (activeFilter.value === 'pending') {
    rows = rows.filter(r => Object.values(r.cells).some(c => c.cell_status === 'pending'))
  } else if (activeFilter.value === 'missing') {
    rows = rows.filter(r => Object.values(r.cells).some(c => c.cell_status === 'missing'))
  } else if (activeFilter.value === 'low') {
    rows = rows.filter(r => r.quoted_count < 2)
  }

  return rows
})

// 对齐核查表不分页：直接全显示（采购项总数已在上方统计卡展示）。

// Filter counts
const needsActionCount = computed(() => result.value?.rows.filter(rowNeedsAction).length ?? 0)
const pendingRowCount = computed(() => result.value?.rows.filter(r =>
  Object.values(r.cells).some(c => c.cell_status === 'pending')
).length ?? 0)
const missingRowCount = computed(() => result.value?.rows.filter(r =>
  Object.values(r.cells).some(c => c.cell_status === 'missing')
).length ?? 0)
const lowCovCount = computed(() => result.value?.rows.filter(r => r.quoted_count < 2).length ?? 0)

// ─── Table columns ────────────────────────────────────────────────────────────
const columns = computed(() => {
  if (!result.value) return []
  const base = [
    { title: '序', dataIndex: 'anchor_seq', key: 'seq', width: 52, fixed: 'left' as const },
    { title: '采购项名称', dataIndex: 'anchor_name', key: 'name', width: 180, fixed: 'left' as const, ellipsis: true },
    { title: '规格型号', dataIndex: 'anchor_spec', key: 'spec', width: 160, ellipsis: true },
    { title: '单位', dataIndex: 'unit', key: 'unit', width: 52 },
    { title: '数量', dataIndex: 'quantity', key: 'qty', width: 60 },
  ]
  const supCols = result.value.suppliers.map((s: ReviewSupplier) => ({
    title: s.supplier_raw_name || s.supplier_name,
    key: `sub_${s.submission_id}`,
    dataIndex: `sub_${s.submission_id}`,
    width: 170,
    customCell: () => ({ style: 'padding: 4px 6px;' }),
  }))
  const tail = [
    { title: '覆盖', key: 'coverage', width: 64 },
  ]
  return [...base, ...supCols, ...tail]
})

// ─── Cell helpers ─────────────────────────────────────────────────────────────
function cellBg(cell: ReviewCell | undefined): string {
  if (!cell) return ''
  switch (cell.cell_status) {
    case 'quoted':
    case 'aggregated':
      return cell.is_lowest ? 'background:#f6ffed' : ''
    case 'pending': return 'background:#fff7e6'
    case 'missing': return 'background:#fafafa'
    case 'excluded': return 'background:#f5f5f5'
    default: return ''
  }
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—'
  return '¥' + v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function fmtConf(v: number | null | undefined): string {
  if (v == null) return ''
  return (v * 100).toFixed(0) + '%'
}

// ─── Actions ─────────────────────────────────────────────────────────────────
async function confirmItem(itemId: number | null | undefined, action: 'align' | 'exclude') {
  if (!itemId) return
  confirmLoading.value[itemId] = true
  try {
    await analysisApi.anchorReviewItemConfirm({ item_id: itemId, action })
    message.success(action === 'align' ? '已纳入矩阵' : '已排除')
    await load()
  } catch {
    message.error('操作失败，请重试')
  } finally {
    delete confirmLoading.value[itemId]
  }
}

async function confirmCandidate(candidateItemId: number, action: 'align' | 'exclude') {
  await confirmItem(candidateItemId, action)
}

function toggleExpand(anchorSeq: string, submissionId: number) {
  const key = `${anchorSeq}_${submissionId}`
  expandedCells.value[key] = !expandedCells.value[key]
}

function isExpanded(anchorSeq: string, submissionId: number): boolean {
  return !!expandedCells.value[`${anchorSeq}_${submissionId}`]
}

// ─── Hard assertion: anchors_total × supplier_count == actual cell count ──────
const cellAccountingOk = computed(() => {
  if (!result.value) return true
  const expected = result.value.anchors_total * result.value.supplier_count
  const actual = result.value.rows.reduce((sum, row) => sum + Object.keys(row.cells).length, 0)
  return actual === expected
})

const cellAccountingDetail = computed(() => {
  if (!result.value) return ''
  const expected = result.value.anchors_total * result.value.supplier_count
  const actual = result.value.rows.reduce((sum, row) => sum + Object.keys(row.cells).length, 0)
  return `期望 ${result.value.anchors_total} × ${result.value.supplier_count} = ${expected} 格，实际 ${actual} 格`
})
</script>

<template>
  <div class="arm">
    <!-- ── Loading ── -->
    <div v-if="loading && !result" style="text-align:center;padding:48px 0">
      <a-spin size="large" />
      <div style="margin-top:12px;color:#666">加载复核矩阵...</div>
    </div>

    <template v-else-if="result">
      <!-- ── Summary bar ── -->
      <div class="arm__summary">
        <div class="arm__stat">
          <span class="arm__stat-val">{{ result.anchors_total }}</span>
          <span class="arm__stat-lbl">采购项</span>
        </div>
        <div class="arm__stat">
          <span class="arm__stat-val">{{ result.supplier_count }}</span>
          <span class="arm__stat-lbl">供应商</span>
        </div>
        <div class="arm__stat" :class="result.pending_cells > 0 ? 'arm__stat--warn' : 'arm__stat--ok'">
          <span class="arm__stat-val">{{ result.pending_cells }}</span>
          <span class="arm__stat-lbl">待确认</span>
        </div>
        <div class="arm__stat" :class="result.missing_cells > 0 ? 'arm__stat--grey' : 'arm__stat--ok'">
          <span class="arm__stat-val">{{ result.missing_cells }}</span>
          <span class="arm__stat-lbl">缺报</span>
        </div>
        <div class="arm__stat arm__stat--blue">
          <span class="arm__stat-val">{{ result.quoted_ge_2_count }}<span style="font-size:12px;font-weight:400">/{{ result.anchors_total }}</span></span>
          <span class="arm__stat-lbl">可比价(≥2家)</span>
        </div>
        <div class="arm__stat arm__stat--blue">
          <span class="arm__stat-val">{{ result.quoted_full_count }}<span style="font-size:12px;font-weight:400">/{{ result.anchors_total }}</span></span>
          <span class="arm__stat-lbl">全供应商</span>
        </div>
        <!-- 业主品牌要求 -->
        <div v-if="result.brand_requirement?.length" style="display:flex;align-items:center;gap:4px;font-size:11px">
          <span style="color:rgba(0,0,0,0.45)">品牌要求：</span>
          <a-tag v-for="b in result.brand_requirement" :key="b.brand_en" color="blue" style="font-size:11px;margin:0">
            {{ b.brand_en }} {{ b.brand_cn }}
          </a-tag>
        </div>
        <!-- checksum warnings -->
        <template v-for="sup in result.suppliers" :key="sup.submission_id">
          <a-tag v-if="sup.checksum_status === 'fail'" color="orange" style="font-size:11px">
            {{ sup.supplier_raw_name || sup.supplier_name }} 核价异常
          </a-tag>
        </template>
        <a-button size="small" :loading="loading" @click="load" style="margin-left:auto">
          <template #icon><ReloadOutlined /></template>
          刷新
        </a-button>
      </div>

      <!-- ── Filter bar ── -->
      <div class="arm__filter">
        <a-radio-group v-model:value="activeFilter" button-style="solid" size="small">
          <a-radio-button value="all">全部 ({{ result.anchors_total }})</a-radio-button>
          <a-radio-button value="needs_action">需处理 ({{ needsActionCount }})</a-radio-button>
          <a-radio-button value="pending">待确认 ({{ pendingRowCount }})</a-radio-button>
          <a-radio-button value="missing">缺报 ({{ missingRowCount }})</a-radio-button>
          <a-radio-button value="low">可比不足 ({{ lowCovCount }})</a-radio-button>
        </a-radio-group>
        <a-input
          v-model:value="searchText"
          placeholder="搜索采购项..."
          style="width:200px"
          allow-clear
        >
          <template #prefix><SearchOutlined /></template>
        </a-input>
      </div>

      <!-- ── Cell accounting hard assertion ── -->
      <a-alert
        v-if="!cellAccountingOk"
        type="error"
        show-icon
        style="margin-bottom:12px"
        message="数据完整性错误：矩阵格数异常，请重新运行匹配"
        :description="cellAccountingDetail"
      />

      <!-- ── Matrix table ── -->
      <div v-if="cellAccountingOk" class="arm__table-wrap">
        <a-table
          :columns="columns"
          :data-source="filteredRows"
          :row-key="(r: ReviewRow) => r.anchor_seq"
          :scroll="{ x: 'max-content', y: 640 }"
          :pagination="false"
          size="small"
          :loading="loading"
          class="arm__table"
        >
          <template #bodyCell="{ column, record }: { column: { key: string }, record: ReviewRow }">

            <!-- Spec (ellipsis tooltip) + 材质/品牌要求 -->
            <template v-if="column.key === 'spec'">
              <a-tooltip :title="[record.anchor_spec, record.anchor_pressure, record.anchor_materials].filter(Boolean).join(' · ')">
                <div style="font-size:11px;color:#555">
                  {{ record.anchor_spec || '—' }}
                  <span v-if="record.anchor_pressure" style="color:#999"> {{ record.anchor_pressure }}</span>
                </div>
                <div v-if="record.anchor_materials" style="font-size:10px;color:#999;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
                  材质：{{ record.anchor_materials }}
                </div>
                <a-tag v-if="record.anchor_brand" color="cyan" style="font-size:10px;padding:0 4px;margin-top:1px">{{ record.anchor_brand }}</a-tag>
              </a-tooltip>
            </template>

            <!-- Quantity -->
            <template v-else-if="column.key === 'qty'">
              <span style="font-size:12px">{{ record.quantity ?? '—' }}</span>
            </template>

            <!-- Coverage column -->
            <template v-else-if="column.key === 'coverage'">
              <a-tag
                :color="record.quoted_count >= 2 ? (record.quoted_count === result!.supplier_count ? 'green' : 'blue') : 'orange'"
                style="font-size:11px;padding:0 4px"
              >
                {{ record.quoted_count }}/{{ result!.supplier_count }}
              </a-tag>
            </template>

            <!-- Supplier cell — keyed by submission_id (§7) -->
            <template v-else-if="column.key.startsWith('sub_')">
              <div
                v-for="sup in result!.suppliers.filter(s => `sub_${s.submission_id}` === column.key)"
                :key="sup.submission_id"
              >
                <div
                  :style="cellBg(record.cells[String(sup.submission_id)])"
                  style="border-radius:4px;padding:4px 6px;min-height:36px"
                >
                  <!-- ── 未报价 ── -->
                  <template v-if="!record.cells[String(sup.submission_id)] || record.cells[String(sup.submission_id)].cell_status === 'missing'">
                    <div style="display:flex;align-items:center;gap:4px">
                      <span style="color:#bbb;font-size:11px">未报价</span>
                      <a-button
                        type="link" size="small"
                        style="font-size:10px;padding:0;height:16px;color:#bbb"
                        @click.stop="toggleExpand(record.anchor_seq, sup.submission_id)"
                      >{{ isExpanded(record.anchor_seq, sup.submission_id) ? '▴' : '▾' }}</a-button>
                    </div>
                    <div v-if="isExpanded(record.anchor_seq, sup.submission_id)"
                      style="margin-top:4px;font-size:10px;color:#999;line-height:1.5;border-top:1px solid #f0f0f0;padding-top:4px">
                      <div>{{ record.cells[String(sup.submission_id)]?.missing_reason || '该供应商未报价此品项' }}</div>
                      <div style="margin-top:4px;display:flex;gap:4px"
                        v-if="!record.cells[String(sup.submission_id)]?.missing_acked">
                        <a-button size="small" style="font-size:10px;height:18px;padding:0 6px"
                          :loading="missingAckLoading[`${record.anchor_seq}_${sup.submission_id}`]"
                          @click.stop="setMissingAck(record.anchor_seq, sup.submission_id, true)"
                        >确认缺报</a-button>
                      </div>
                      <div v-else style="color:#52c41a;font-size:10px;margin-top:2px;display:flex;align-items:center;gap:6px">
                        <span>✓ 已确认缺报</span>
                        <a-button type="link" size="small" style="font-size:10px;padding:0;height:auto;color:#8c8c8c"
                          :loading="missingAckLoading[`${record.anchor_seq}_${sup.submission_id}`]"
                          @click.stop="setMissingAck(record.anchor_seq, sup.submission_id, false)"
                        >取消确认</a-button>
                      </div>
                    </div>
                  </template>

                  <!-- ── 已排除 ── -->
                  <template v-else-if="record.cells[String(sup.submission_id)].cell_status === 'excluded'">
                    <span style="color:#bbb;font-size:12px;text-decoration:line-through">已排除</span>
                  </template>

                  <!-- ── 待确认 ── -->
                  <template v-else-if="record.cells[String(sup.submission_id)].cell_status === 'pending'">
                    <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
                      <a-tag color="orange" style="font-size:10px;padding:0 4px;margin:0">待确认</a-tag>
                      <a-tooltip v-if="record.cells[String(sup.submission_id)].evidence"
                        :title="record.cells[String(sup.submission_id)].evidence">
                        <span style="font-size:12px;color:#d46b08;font-weight:600;cursor:help">
                          {{ fmtPrice(record.cells[String(sup.submission_id)].unit_price) }}
                        </span>
                      </a-tooltip>
                      <span v-else style="font-size:12px;color:#d46b08;font-weight:600">
                        {{ fmtPrice(record.cells[String(sup.submission_id)].unit_price) }}
                      </span>
                      <span v-if="record.cells[String(sup.submission_id)].confidence != null"
                        style="font-size:10px;color:#999">
                        {{ fmtConf(record.cells[String(sup.submission_id)].confidence) }}
                      </span>
                    </div>
                    <div style="display:flex;gap:4px;margin-top:4px;align-items:center">
                      <a-button
                        type="primary" size="small"
                        style="font-size:11px;padding:0 6px;height:20px"
                        :loading="confirmLoading[record.cells[String(sup.submission_id)].item_id!]"
                        @click.stop="confirmItem(record.cells[String(sup.submission_id)].item_id, 'align')"
                      >✓ 纳入</a-button>
                      <a-button
                        danger size="small"
                        style="font-size:11px;padding:0 6px;height:20px"
                        :loading="confirmLoading[record.cells[String(sup.submission_id)].item_id!]"
                        @click.stop="confirmItem(record.cells[String(sup.submission_id)].item_id, 'exclude')"
                      >✗ 排除</a-button>
                      <a-button
                        v-if="record.cells[String(sup.submission_id)].candidates?.length > 1"
                        size="small"
                        style="font-size:10px;padding:0 4px;height:20px"
                        @click.stop="toggleExpand(record.anchor_seq, sup.submission_id)"
                      >换候选</a-button>
                    </div>
                    <!-- Candidates expand -->
                    <div v-if="isExpanded(record.anchor_seq, sup.submission_id)"
                      style="margin-top:6px;border-top:1px solid #ffe7ba;padding-top:4px">
                      <div
                        v-for="cand in record.cells[String(sup.submission_id)].candidates"
                        :key="cand.item_id"
                        style="display:flex;align-items:center;gap:4px;padding:2px 0;font-size:11px"
                        :style="cand.item_id === record.cells[String(sup.submission_id)].item_id ? 'background:#fff7e6;border-radius:2px;padding:2px 4px' : ''"
                      >
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                          {{ cand.material_name }}
                          <span style="color:#999"> {{ cand.spec }}</span>
                        </span>
                        <span style="color:#d46b08;white-space:nowrap">{{ fmtPrice(cand.unit_price) }}</span>
                        <span style="color:#bbb;white-space:nowrap">{{ fmtConf(cand.confidence) }}</span>
                        <a-button
                          type="link" size="small"
                          style="font-size:10px;padding:0;height:16px"
                          :loading="confirmLoading[cand.item_id]"
                          @click.stop="confirmCandidate(cand.item_id, 'align')"
                        >选此条</a-button>
                      </div>
                    </div>
                  </template>

                  <!-- ── 已确认/聚合 ── -->
                  <template v-else>
                    <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
                      <CheckCircleOutlined v-if="record.cells[String(sup.submission_id)].cell_status === 'quoted'"
                        style="color:#52c41a;font-size:11px" />
                      <a-tag v-else color="cyan" style="font-size:10px;padding:0 4px;margin:0">聚合</a-tag>
                      <span
                        style="font-size:13px;font-weight:600"
                        :style="record.cells[String(sup.submission_id)].is_lowest ? 'color:#389e0d' : ''"
                      >
                        {{ fmtPrice(record.cells[String(sup.submission_id)].unit_price) }}
                      </span>
                      <span v-if="record.cells[String(sup.submission_id)].is_lowest"
                        style="font-size:10px;color:#389e0d">最低</span>
                      <a-tooltip v-if="record.cells[String(sup.submission_id)].evidence"
                        :title="record.cells[String(sup.submission_id)].evidence">
                        <a style="font-size:10px;color:#bbb;cursor:help">证据</a>
                      </a-tooltip>
                    </div>
                  </template>
                </div>
              </div>
            </template>

          </template>

          <!-- Supplier column header -->
          <template #headerCell="{ column }: { column: { key: string; title: string } }">
            <template v-if="column.key.startsWith('sub_')">
              <div v-for="sup in result!.suppliers.filter(s => `sub_${s.submission_id}` === column.key)" :key="sup.submission_id">
                <div>
                  {{ sup.supplier_raw_name || sup.supplier_name }}
                  <a-tag
                    v-if="sup.checksum_status === 'fail'"
                    color="orange"
                    style="font-size:10px;padding:0 3px;margin-left:4px"
                  >核价待查</a-tag>
                </div>
                <a-tag v-if="sup.brand" color="cyan" style="font-size:10px;padding:0 4px;margin:2px 0 0;font-weight:400">
                  品牌：{{ sup.brand }}
                </a-tag>
              </div>
            </template>
          </template>

        </a-table>
      </div>

      <!-- ── Empty state ── -->
      <div v-if="cellAccountingOk && !loading && filteredRows.length === 0" style="text-align:center;padding:24px;color:#999">
        <a-empty description="当前筛选条件下无数据" />
      </div>

    </template>

    <div v-else-if="!loading" style="text-align:center;padding:48px 0;color:#bbb">
      加载中...
    </div>
  </div>
</template>

<style scoped>
.arm {
  margin-top: 16px;
}

.arm__summary {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 16px;
  background: #fafafa;
  border-radius: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.arm__stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 56px;
}

.arm__stat-val {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.2;
}

.arm__stat-lbl {
  font-size: 11px;
  color: rgba(0, 0, 0, 0.45);
  margin-top: 1px;
}

.arm__stat--warn .arm__stat-val { color: #fa8c16; }
.arm__stat--ok .arm__stat-val { color: #52c41a; }
.arm__stat--grey .arm__stat-val { color: #8c8c8c; }
.arm__stat--blue .arm__stat-val { color: #1677ff; }

.arm__filter {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.arm__table-wrap {
  overflow: hidden;
  border-radius: 6px;
  border: 1px solid #f0f0f0;
}

.arm__table :deep(.ant-table-cell) {
  vertical-align: top;
  padding: 4px 6px !important;
}

.arm__table :deep(.ant-table-thead .ant-table-cell) {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  background: #fafafa;
}

.arm__table :deep(.ant-table-row:hover .ant-table-cell) {
  background: inherit !important;
}
</style>
