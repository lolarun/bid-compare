// MEMPAS 偏差色标三级化
// 对应 docs/design/06-功能设计.md F5.1 / F6.2 与 07-技术设计.md §5.2

export type AlertLevel = 'normal' | 'yellow' | 'red'

export const alertColors: Record<AlertLevel, string> = {
  normal: '#52c41a',
  yellow: '#faad14',
  red: '#ff4d4f',
}

export const alertLabels: Record<AlertLevel, string> = {
  normal: '正常',
  yellow: '需关注',
  red: '异常',
}

/**
 * 根据偏差率（小数形式，0.07 = 7%）与阈值，判定色标等级。
 * 默认阈值 yellow=0.05, red=0.10。
 */
export function classifyAlert(
  deviation: number | null | undefined,
  yellow = 0.05,
  red = 0.10,
): AlertLevel {
  if (deviation == null || Number.isNaN(deviation)) return 'normal'
  const abs = Math.abs(deviation)
  if (abs <= yellow) return 'normal'
  if (abs <= red) return 'yellow'
  return 'red'
}

/** 兼容后端历史返回值：blue / green 一律折叠到 normal/red */
export function normalizeAlert(raw?: string | null): AlertLevel {
  switch (raw) {
    case 'red':
      return 'red'
    case 'yellow':
      return 'yellow'
    case 'normal':
    case 'green':
      return 'normal'
    // 旧的 blue（异常偏低）按设计文档合并入 red
    case 'blue':
      return 'red'
    default:
      return 'normal'
  }
}

export function formatDeviation(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const pct = (value * 100).toFixed(1)
  return value >= 0 ? `+${pct}%` : `${pct}%`
}
