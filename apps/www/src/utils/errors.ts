/**
 * R5（低垂果实轮）：从 IndexView.vue 抽出——原先只有 runTenderMatch /
 * loadAnchorReview / removeBatchEntry / removeAllBatchEntries 四处调用，
 * 现在报价上传逻辑搬进 composables/useSupplierUpload.ts，两个文件都要用，
 * 不能再是某个 .vue 里的私有函数。
 */

/**
 * 从 axios 错误里取一条可读消息：detail 可能是字符串，也可能是结构化对象
 * （如质量门 409 的 {error, message, failures}）。直接 message.error(对象) 会渲染成
 * "[object Object]"，这里统一抽取 message/error 字段或 JSON 兜底。
 */
export function extractErrMsg(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof d === 'string') return d
  if (d && typeof d === 'object') {
    const o = d as { message?: unknown; error?: unknown }
    if (typeof o.message === 'string') return o.message
    if (typeof o.error === 'string') return o.error
    try { return JSON.stringify(d) } catch { return fallback }
  }
  return fallback
}
