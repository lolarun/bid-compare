import { message } from 'ant-design-vue'
import type { AxiosResponse } from 'axios'

/**
 * Trigger a browser file download from an Axios blob response.
 * Falls back to Content-Disposition header filename, then uses the provided default.
 */
export function downloadBlob(resp: AxiosResponse, fallbackName = 'export.xlsx') {
  const disposition = resp.headers['content-disposition'] || ''
  const match = disposition.match(/filename\*?=(?:UTF-8''|")?(.*?)(?:"|;|$)/i)
  const filename = match ? decodeURIComponent(match[1]) : fallbackName

  const url = window.URL.createObjectURL(new Blob([resp.data]))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  window.URL.revokeObjectURL(url)
}

/**
 * Wrap an export API call with loading message + error handling.
 */
export async function doExport(
  apiFn: () => Promise<AxiosResponse>,
  fallbackName?: string,
) {
  const hide = message.loading('正在生成导出文件...', 0)
  try {
    const resp = await apiFn()
    downloadBlob(resp, fallbackName)
    message.success('导出成功')
  } catch (e: unknown) {
    console.error('Export failed', e)
    message.error('导出失败，请稍后重试')
  } finally {
    hide()
  }
}
