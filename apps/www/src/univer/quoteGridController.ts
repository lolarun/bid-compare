/**
 * design/27 §5/§10 步骤2 —— Univer 报价表格的唯一接触点。
 *
 * 复核意见（2026-08-13）："Univer 的 API 调用集中在一个模块里（数据装载/
 * 标色/读回编辑三个入口），业务代码不直接摸 Univer 对象——万一那 1% 的概率
 * 兑现要换 AG Grid，换的是一个文件不是一片。"本文件就是那"一个文件"：
 * `QuoteGrid.vue`（以及未来接它的工作台代码）只调用这里导出的函数/类型，
 * 从不 import `@univerjs/*` 或碰 `FWorkbook`/`FRange` 这类 Univer 对象。
 *
 * 三个入口：
 * 1. `mount()` —— 创建实例 + 装载数据（初次进入即完成，不分两步）。
 * 2. `applyDoubtMarks()` —— 单元格标色（design/27 §4 三色判据）。
 * 3. `handle.readRows()` / `onRowsChanged` —— 读回编辑后的数据。
 */

export interface QuoteGridColumn {
  key: string          // QuoteExtractionItem 的字段名
  title: string        // 表头人话
  width?: number
}

export type DoubtSeverity = 'missing' | 'arithmetic' | 'truncation'

export interface DoubtMark {
  row: number           // 0-based，对应装载时传入数组的下标（不含表头行）
  columnKey: string      // 对应 QuoteGridColumn.key
  severity: DoubtSeverity
  hoverText: string      // 人话解释，design/27 §4："第69行没读到数量"这类
}

const SEVERITY_COLOR: Record<DoubtSeverity, string> = {
  missing: '#ff4d4f',      // 红：缺数量/缺关键字段
  arithmetic: '#faad14',   // 黄：数量×单价≠合价
  truncation: '#fa8c16',   // 橙：数值疑似被截断
}

export interface QuoteGridHandle {
  /** 覆盖式重新装载整表数据（供应商切换、重新识别后调用）。 */
  loadRows: (rows: Record<string, unknown>[]) => void
  /** 应用/刷新单元格标色——每次调用先清空旧标色再画新的，不用调用方自己清。 */
  applyDoubtMarks: (marks: DoubtMark[]) => void
  /** 读回当前表格内容（含用户编辑），行序与装载时一致。 */
  readRows: () => Record<string, unknown>[]
  /** 编辑发生时的回调——用于跟父组件的 v-model 同步（ExtractionEditor 同款契约）。 */
  onRowsChanged: (cb: (rows: Record<string, unknown>[]) => void) => void
  dispose: () => void
}

/**
 * 创建一个 Univer 报价表格实例并装载初始数据。
 *
 * `container` 必须已经在 DOM 里（不能是还没 mount 的 ref）。列宽/表头按
 * `columns` 顺序排布；数据行按 `columns[].key` 从每行对象里取值，取不到的
 * 字段留空，不报错——识别结果本来就有字段缺失的正常情况（§4 判据是标色，
 * 不是报错）。
 */
export async function mountQuoteGrid(
  container: HTMLElement,
  columns: QuoteGridColumn[],
  initialRows: Record<string, unknown>[],
): Promise<QuoteGridHandle> {
  const { createUniver, LocaleType, mergeLocales } = await import('@univerjs/presets')
  const { UniverSheetsCorePreset } = await import('@univerjs/preset-sheets-core')
  const { UniverSheetsNotePreset } = await import('@univerjs/preset-sheets-note')
  const zhCN = (await import('@univerjs/preset-sheets-core/locales/zh-CN')).default
  await import('@univerjs/preset-sheets-core/lib/index.css')
  await import('@univerjs/preset-sheets-note/lib/index.css')

  const { univer, univerAPI } = createUniver({
    locale: LocaleType.ZH_CN,
    locales: { [LocaleType.ZH_CN]: mergeLocales(zhCN) },
    presets: [
      UniverSheetsCorePreset({
        container,
        header: false,
        toolbar: false,
        footer: false,
      }),
      // 悬浮解释（design/27 §4）：轻量批注插件，比 sheets-thread-comment 那套
      // 协作评论系统更贴合"悬浮一行字"这个需求，不引入协作相关的额外重量。
      UniverSheetsNotePreset(),
    ],
  })
  univerAPI.createWorkbook({})

  const fWorkbook = univerAPI.getActiveWorkbook()
  if (!fWorkbook) throw new Error('Univer createWorkbook 未返回工作簿')
  const fWorksheet = fWorkbook.getActiveSheet()

  // CellValue（Univer 单元格值类型）就是 string|number|boolean|null 的并集，
  // 跟这里 unknown[][] 的运行时形状完全一致，只是 TS 推不出来——每次 setValues
  // 调用点用 as any 显式收窄，比把整个模块的类型都放宽成 any 更小范围。
  function toGrid(rows: Record<string, unknown>[]): unknown[][] {
    const header = columns.map((c) => c.title)
    const body = rows.map((r) => columns.map((c) => r[c.key] ?? ''))
    return [header, ...body]
  }

  // 上一次实际写入的行数（不含表头）——清空旧数据时用它而不是写死的数字：
  // 写死的数字（曾经是 2000）会超出工作表默认行数上限（1000）直接报
  // "Range is out of bounds"；用实际写过的行数，需要更多时才扩容，永远不会
  // 凭空要求一个从没存在过的范围。
  let currentRowCount = 0

  function writeRows(rows: Record<string, unknown>[]) {
    const grid = toGrid(rows)
    // 先清空再写：行数可能比上一次少（供应商切换），残留旧行会显示成"幽灵数据"。
    const needed = Math.max(grid.length, currentRowCount)
    if (needed > fWorksheet.getMaxRows()) fWorksheet.setRowCount(needed)
    fWorksheet.getRange(0, 0, needed, columns.length).clearContent()
    fWorksheet.getRange(0, 0, grid.length, columns.length).setValues(grid as any)
    currentRowCount = rows.length
  }

  writeRows(initialRows)

  function clearMarks() {
    if (currentRowCount === 0) return
    // setBackground(color) 只接受 string，没有"传 null 清空"的重载——空字符串
    // 是这套 Facade API 里"恢复默认背景"的约定用法（对照 setFontColor 等同款
    // 签名的其余方法）。
    fWorksheet.getRange(1, 0, currentRowCount, columns.length).setBackground('')
  }

  function applyDoubtMarks(marks: DoubtMark[]) {
    clearMarks()
    for (const m of marks) {
      const colIdx = columns.findIndex((c) => c.key === m.columnKey)
      if (colIdx < 0) continue
      // Univer 行号 0 = 表头，数据第 m.row 行（0-based）落在 sheet 第 m.row+1 行。
      const cell = fWorksheet.getRange(m.row + 1, colIdx, 1, 1)
      cell.setBackgroundColor(SEVERITY_COLOR[m.severity])
      // 悬浮解释（design/27 §4）：@univerjs/sheets-note 的轻量批注——Excel 熟悉
      // 的"红三角+悬浮显示"交互，不是 sheets-thread-comment 那套协作评论系统。
      cell.createOrUpdateNote({
        id: `doubt-${m.row}-${colIdx}`, row: m.row + 1, col: colIdx,
        note: m.hoverText, width: 220, height: 60, show: false,
      })
    }
  }

  function readRows(): Record<string, unknown>[] {
    const values = fWorksheet.getRange(1, 0, currentRowCount, columns.length).getValues() as unknown[][]
    return values.map((row) => {
      const out: Record<string, unknown> = {}
      columns.forEach((c, i) => { out[c.key] = row[i] })
      return out
    })
  }

  const changeListeners: Array<(rows: Record<string, unknown>[]) => void> = []
  const offValueChanged = univerAPI.addEvent(univerAPI.Event.SheetValueChanged, () => {
    const rows = readRows()
    changeListeners.forEach((cb) => cb(rows))
  })

  return {
    loadRows: writeRows,
    applyDoubtMarks,
    readRows,
    onRowsChanged: (cb) => { changeListeners.push(cb) },
    dispose: () => {
      offValueChanged?.dispose?.()
      univer.dispose()
    },
  }
}
