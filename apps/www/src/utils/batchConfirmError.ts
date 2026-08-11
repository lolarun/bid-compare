/**
 * 评审 R2（第3块）：/api/quotes/batch-confirm 的两个结构化错误
 * （missing_total_requires_review / declared_total_mismatch）此前只落进
 * extractErrMsg 的通用 toast——没有 ack 参数、没有复核清单 UI，
 * 结构化 detail 里除 message 外的字段全部被扔掉。
 *
 * 这里把两个已知错误形状解析成可判别联合类型，供调用方渲染专属 Modal
 * （而不是继续走裸文本 message.error）。未知/非结构化错误落 'other'，
 * 行为与此前的 extractErrMsg 一致，不收窄兼容面。
 */

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
 * block instead of duplicating the window.confirm/alert copy.
 *
 * Returns true when the user reviewed a declared-total mismatch and chose to
 * force the submit through (`checksum_ack=true`) — the caller must retry with
 * that flag itself, this function does not know how to resubmit for every
 * caller's slightly different request shape. missing_total has no ack switch
 * (the server never allows silently skipping rows with no total) so it only
 * informs and always returns false.
 */
export async function handleBatchConfirmError(e: unknown, msg: MessageApi): Promise<boolean> {
  const parsed = parseBatchConfirmError(e, '入库失败')
  if (parsed.kind === 'checksum_mismatch') {
    const c = parsed.checksum
    const declaredText = c.declared != null ? `¥${c.declared.toLocaleString()}` : '（文件未给出声明总价）'
    return window.confirm(
      `${parsed.message}\n\n` +
      `文件声明总价：${declaredText}\n` +
      `明细合价之和：¥${c.line_sum.toLocaleString()}\n` +
      (c.delta_pct != null ? `差异：${c.delta_pct}%（允许阈值 ${c.threshold_pct ?? '?'}%）\n` : '') +
      `\n核对无误后点「确定」按明细合价强制入库；点「取消」返回表格核对。`,
    )
  }
  if (parsed.kind === 'missing_total') {
    const lines = parsed.rows.slice(0, 20).map((r) =>
      `第${r.index}行 ${r.material}${r.spec ? '（' + r.spec + '）' : ''}：原文无合价` +
      (r.derived_total_candidate != null
        ? `（数量×单价≈${r.derived_total_candidate}，仅供参考，不代表原文）`
        : ''),
    ).join('\n')
    window.alert(
      `${parsed.message}\n\n${lines}` +
      (parsed.totalCount > 20 ? `\n……等共 ${parsed.totalCount} 行` : '') +
      `\n\n请返回表格为这些行补写合价，或确认原文确实未报价后再重新点击「校对入库」。`,
    )
    return false
  }
  msg.error(parsed.message)
  return false
}
