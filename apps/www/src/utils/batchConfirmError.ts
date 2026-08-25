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
  // 2026-08-23：原文备注/核对说明随行带上——真实语料里出现过原表在单价/合价
  // 格子印"/"（明确不报价），但转换成 CSV/Excel 后这个符号只留在备注列文字里，
  // 价格两格本身变空白。没有这条，人要判断"是不是明确不报价"就得重新翻原文。
  remark: string
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
      remark: typeof r.remark === 'string' ? r.remark : '',
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
 *
 * `onConfirmNotQuoted`（可选，2026-08-23 新增）：missing_total 场景下，用户核对
 * 备注后确认"这些行原文确实未报价"时调用，参数是这些行在 `overrides` 数组里的
 * 下标。**在此之前这个对话框只有一个"去核对这些行"按钮**——它只会跳转页面，
 * 不做任何事；提示语却写着"或确认原文确实未报价后再重新点击「校对入库」"，
 * 界面上根本没有做这件事的控件，用户点了跟没点一样。后端其实早就认
 * `item.not_quoted` 这个字段（不阻断），缺的只是前端把"我确认了"这句话传过去。
 *
 * 只有**这次弹窗里显示的行数覆盖了全部待确认行**（`rows.length >= totalCount`）
 * 才提供这个批量按钮——弹窗最多显示 50 行，真遇到更多就不出现这个按钮，逼着走
 * 逐行核对：**绝不能替用户确认一行他根本没看到的数据**。返回 `true` 复用跟
 * checksum 强制入库同一条"请调用方重试"协议，调用方在收到 `true` 前已经通过
 * 回调把 `not_quoted` 写回了 items，重试即可拿到新结果。
 */
export async function handleBatchConfirmError(
  e: unknown,
  msg: MessageApi,
  onViewDetails?: () => void,
  onConfirmNotQuoted?: (indexes: number[]) => void,
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
        : '') +
      // 备注是判断"是不是明确不报价"最直接的证据（见下方 `onConfirmNotQuoted`
      // 文档）——原来这里只显示 material/spec/derived_total，备注字段存在却
      // 从没被读过，人要么凭空猜、要么去翻原文。
      (r.remark ? `\n    原文备注：${r.remark}` : ''),
    ).join('\n')
    const truncated = parsed.totalCount > 20
    // 只有这次弹窗真的显示了全部待确认行，才提供"批量确认未报价"——弹窗最多
    // 显示 20 行文字，`rows` 最多携带 50 行下标；只要 totalCount 超过 20，
    // 就说明至少一行没有被用户看到，不能替他确认。
    const canBulkConfirm = Boolean(onConfirmNotQuoted) && !truncated
    return new Promise<boolean>((resolve) => {
      if (canBulkConfirm) {
        Modal.confirm({
          title: parsed.message,
          width: 560,
          content: h('div', { style: 'white-space:pre-line;max-height:320px;overflow:auto;font-size:12px' },
            lines + '\n\n如果核对备注后确认这些行原文确实未报价，点「确认未报价」直接入库；' +
            '需要先补写合价或原文没有这个说法，点「去核对这些行」。'),
          okText: '确认未报价，继续入库',
          cancelText: onViewDetails ? '去核对这些行' : '取消',
          onOk: () => { onConfirmNotQuoted!(parsed.rows.map((r) => r.index)); resolve(true) },
          onCancel: () => { onViewDetails?.(); resolve(false) },
        })
      } else {
        Modal.info({
          title: parsed.message,
          width: 560,
          content: h('div', { style: 'white-space:pre-line;max-height:320px;overflow:auto;font-size:12px' },
            lines + (truncated ? `\n……等共 ${parsed.totalCount} 行` : '') +
            '\n\n请为这些行补写合价，或确认原文确实未报价后再重新点击「校对入库」。'),
          okText: onViewDetails ? '去核对这些行' : '知道了',
          onOk: () => { onViewDetails?.(); resolve(false) },
        })
      }
    })
  }
  msg.error(parsed.message)
  return false
}
