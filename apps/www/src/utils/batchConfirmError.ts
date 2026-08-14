/**
 * 评审 R2（第3块）：/api/quotes/batch-confirm 的两个结构化错误
 * （missing_total_requires_review / declared_total_mismatch）此前只落进
 * extractErrMsg 的通用 toast——没有 ack 参数、没有复核清单 UI，
 * 结构化 detail 里除 message 外的字段全部被扔掉。
 *
 * 这里把两个已知错误形状解析成可判别联合类型，供调用方渲染专属 Modal
 * （而不是继续走裸文本 message.error）。未知/非结构化错误落 'other'，
 * 行为与此前的 extractErrMsg 一致，不收窄兼容面。
 *
 * 2026-08-13 手测发现：这里长期用的是原生 window.confirm/alert，且 missing_total
 * 的文案让用户"返回表格核对"——但批量卡片流程当时压根没有可返回的表格
 * （ExtractionEditor 只接在没人用的 legacy 单供应商 tab 分支）。改成 Ant Design
 * Modal（非原生弹窗，样式与全局一致），并加一个可选 onViewDetails 回调，让调用方
 * 把"去核对"按钮接到自己的行级编辑器展开逻辑上，而不是继续指向一个不存在的地方。
 */
import { h } from 'vue'
import { Modal } from 'ant-design-vue'

interface ErrDetail {
  error?: unknown
  message?: unknown
  checksum?: unknown
  review_rows?: unknown
  review_row_count?: unknown
}

export interface ChecksumInfo {
  declared: number | null
  line_sum: number
  delta_pct: number | null
  status: string
  line_count: number
  threshold_pct?: number
}

export interface MissingTotalRow {
  index: number
  material: string
  spec: string
  qty: number | null
  unit_price: number | null
  derived_total_candidate: number | null
  reason: string
}

export type BatchConfirmError =
  | { kind: 'checksum_mismatch'; message: string; checksum: ChecksumInfo }
  | { kind: 'missing_total'; message: string; rows: MissingTotalRow[]; totalCount: number }
  | { kind: 'other'; message: string }

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Extracts response.data.detail from an axios error, best-effort. */
function detailOf(e: unknown): unknown {
  return (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
}

export function parseBatchConfirmError(e: unknown, fallback: string): BatchConfirmError {
  const d = detailOf(e)
  if (typeof d === 'string') return { kind: 'other', message: d }
  if (!isObj(d)) return { kind: 'other', message: fallback }
  const detail = d as ErrDetail
  const message = typeof detail.message === 'string' ? detail.message : fallback

  if (detail.error === 'declared_total_mismatch' && isObj(detail.checksum)) {
    const c = detail.checksum as Record<string, unknown>
    return {
      kind: 'checksum_mismatch',
      message,
      checksum: {
        declared: typeof c.declared === 'number' ? c.declared : null,
        line_sum: typeof c.line_sum === 'number' ? c.line_sum : 0,
        delta_pct: typeof c.delta_pct === 'number' ? c.delta_pct : null,
        status: typeof c.status === 'string' ? c.status : 'unknown',
        line_count: typeof c.line_count === 'number' ? c.line_count : 0,
        threshold_pct: typeof c.threshold_pct === 'number' ? c.threshold_pct : undefined,
      },
    }
  }

  if (detail.error === 'missing_total_requires_review' && Array.isArray(detail.review_rows)) {
    const rows: MissingTotalRow[] = detail.review_rows.filter(isObj).map((r) => ({
      index: typeof r.index === 'number' ? r.index : 0,
      material: typeof r.material === 'string' ? r.material : '',
      spec: typeof r.spec === 'string' ? r.spec : '',
      qty: typeof r.qty === 'number' ? r.qty : null,
      unit_price: typeof r.unit_price === 'number' ? r.unit_price : null,
      derived_total_candidate: typeof r.derived_total_candidate === 'number' ? r.derived_total_candidate : null,
      reason: typeof r.reason === 'string' ? r.reason : '',
    }))
    return {
      kind: 'missing_total',
      message,
      rows,
      totalCount: typeof detail.review_row_count === 'number' ? detail.review_row_count : rows.length,
    }
  }

  if (typeof detail.message === 'string') return { kind: 'other', message: detail.message }
  if (typeof detail.error === 'string') return { kind: 'other', message: detail.error }
  return { kind: 'other', message: fallback }
}

/** Minimal shape of ant-design-vue's `message` API this helper needs. */
interface MessageApi {
  error: (content: string) => void
}

/**
 * Shared UI reaction for the two structured batch-confirm errors. Both
 * compare/IndexView.vue and import/IndexView.vue call this from their catch
 * block instead of duplicating the modal copy.
 *
 * Returns true when the user reviewed a declared-total mismatch and chose to
 * force the submit through (`checksum_ack=true`) — the caller must retry with
 * that flag itself, this function does not know how to resubmit for every
 * caller's slightly different request shape. missing_total has no ack switch
 * (the server never allows silently skipping rows with no total) so it only
 * informs and always returns false.
 *
 * `onViewDetails`（可选）：missing_total 场景下，用户点"去核对这些行"时调用——
 * 调用方应据此展开/滚动到对应文件的行级编辑器。不传时退化为一个纯"知道了"提示，
 * 行为等价于旧版 window.alert（信息不丢，只是没有跳转动作）。
 */
export async function handleBatchConfirmError(
  e: unknown,
  msg: MessageApi,
  onViewDetails?: () => void,
): Promise<boolean> {
  const parsed = parseBatchConfirmError(e, '入库失败')
  if (parsed.kind === 'checksum_mismatch') {
    const c = parsed.checksum
    const declaredText = c.declared != null ? `¥${c.declared.toLocaleString()}` : '（文件未给出声明总价）'
    return new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: parsed.message,
        content: h('div', { style: 'white-space:pre-line' },
          `文件声明总价：${declaredText}\n` +
          `明细合价之和：¥${c.line_sum.toLocaleString()}\n` +
          (c.delta_pct != null ? `差异：${c.delta_pct}%（允许阈值 ${c.threshold_pct ?? '?'}%）\n` : '') +
          `\n核对无误后点「确定强制入库」；点「取消」返回核对明细。`),
        okText: '确定强制入库',
        cancelText: '取消，去核对',
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      })
    })
  }
  if (parsed.kind === 'missing_total') {
    const lines = parsed.rows.slice(0, 20).map((r) =>
      `第${r.index}行 ${r.material}${r.spec ? '（' + r.spec + '）' : ''}：原文无合价` +
      (r.derived_total_candidate != null
        ? `（数量×单价≈${r.derived_total_candidate}，仅供参考，不代表原文）`
        : ''),
    ).join('\n')
    return new Promise<boolean>((resolve) => {
      Modal.info({
        title: parsed.message,
        width: 560,
        content: h('div', { style: 'white-space:pre-line;max-height:320px;overflow:auto;font-size:12px' },
          lines + (parsed.totalCount > 20 ? `\n……等共 ${parsed.totalCount} 行` : '') +
          '\n\n请为这些行补写合价，或确认原文确实未报价后再重新点击「校对入库」。'),
        okText: onViewDetails ? '去核对这些行' : '知道了',
        onOk: () => { onViewDetails?.(); resolve(false) },
      })
    })
  }
  msg.error(parsed.message)
  return false
}
