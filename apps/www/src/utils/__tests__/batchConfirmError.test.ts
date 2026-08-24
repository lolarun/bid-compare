/**
 * handleBatchConfirmError 的 missing_total 分支（2026-08-23）。
 *
 * `Modal.confirm`/`Modal.info` 是 ant-design-vue 的命令式静态方法——真弹一个
 * 到 happy-dom 里测起来又慢又脆。这里 mock 掉它们，直接抓传给它们的 config
 * 对象、手动调用 onOk/onCancel，测的是**分支逻辑本身**：什么时候给出批量
 * 确认按钮、按钮点了之后传给回调的下标对不对、resolve 的值对不对——这些
 * 恰恰是这次要修的东西，不是 antd 弹窗渲不渲得出来。
 */
import { describe, expect, it, vi } from 'vitest'

const confirmSpy = vi.fn()
const infoSpy = vi.fn()
vi.mock('ant-design-vue', () => ({
  Modal: { confirm: (cfg: unknown) => confirmSpy(cfg), info: (cfg: unknown) => infoSpy(cfg) },
}))

import { handleBatchConfirmError } from '../batchConfirmError'

const msg = { error: vi.fn() }

function missingTotalError(rows: unknown[], totalCount: number) {
  return {
    response: {
      data: {
        detail: {
          error: 'missing_total_requires_review',
          message: `${rows.length} 行原文无合价`,
          review_rows: rows,
          review_row_count: totalCount,
        },
      },
    },
  }
}

const ROW_WITH_REMARK = {
  index: 0, material: '普通电缆', spec: 'HYA-2*0.5', qty: 243.35,
  unit_price: null, derived_total_candidate: null, reason: '原文无合价；需人工确认后方可入库',
  remark: 'PDF原表单价、合价均标为/',
}

describe('handleBatchConfirmError · missing_total', () => {
  it('提供 onConfirmNotQuoted 且弹窗显示了全部行时，给出批量确认按钮', async () => {
    const onConfirmNotQuoted = vi.fn()
    const p = handleBatchConfirmError(
      missingTotalError([ROW_WITH_REMARK], 1), msg, undefined, onConfirmNotQuoted)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(infoSpy).not.toHaveBeenCalled()
    const cfg = confirmSpy.mock.calls[0][0] as { okText: string; onOk: () => void }
    expect(cfg.okText).toContain('确认未报价')
    cfg.onOk()
    expect(onConfirmNotQuoted).toHaveBeenCalledWith([0])
    expect(await p).toBe(true)   // 复用 checksum 那条"请调用方重试"协议
  })

  it('取消时调用 onViewDetails，不调用 onConfirmNotQuoted，resolve(false)', async () => {
    confirmSpy.mockClear()
    const onViewDetails = vi.fn()
    const onConfirmNotQuoted = vi.fn()
    const p = handleBatchConfirmError(
      missingTotalError([ROW_WITH_REMARK], 1), msg, onViewDetails, onConfirmNotQuoted)
    const cfg = confirmSpy.mock.calls[0][0] as { onCancel: () => void }
    cfg.onCancel()
    expect(onViewDetails).toHaveBeenCalledTimes(1)
    expect(onConfirmNotQuoted).not.toHaveBeenCalled()
    expect(await p).toBe(false)
  })

  it('没有 onConfirmNotQuoted 回调时退回旧的纯提示（Modal.info）', async () => {
    confirmSpy.mockClear(); infoSpy.mockClear()
    const p = handleBatchConfirmError(missingTotalError([ROW_WITH_REMARK], 1), msg)
    expect(infoSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy).not.toHaveBeenCalled()
    const cfg = infoSpy.mock.calls[0][0] as { onOk: () => void }
    cfg.onOk()
    expect(await p).toBe(false)
  })

  it('待确认行数超过弹窗能显示的数量时，绝不批量确认没被用户看到的行', async () => {
    infoSpy.mockClear()
    const onConfirmNotQuoted = vi.fn()
    // totalCount(99) 远大于这次弹窗里携带的行数(1)——某些行用户根本没看到。
    const p = handleBatchConfirmError(
      missingTotalError([ROW_WITH_REMARK], 99), msg, undefined, onConfirmNotQuoted)
    expect(infoSpy).toHaveBeenCalledTimes(1)   // 退回纯提示，不给批量按钮
    expect(confirmSpy).not.toHaveBeenCalled()
    infoSpy.mock.calls[0][0].onOk()
    expect(onConfirmNotQuoted).not.toHaveBeenCalled()
    await p
  })

  it('弹窗内容带上原文备注，不再是从后端读到却从不显示', () => {
    confirmSpy.mockClear()
    void handleBatchConfirmError(missingTotalError([ROW_WITH_REMARK], 1), msg, undefined, vi.fn())
    const cfg = confirmSpy.mock.calls[0][0] as { content: { children: string } }
    expect(cfg.content.children).toContain('PDF原表单价、合价均标为/')
  })
})
