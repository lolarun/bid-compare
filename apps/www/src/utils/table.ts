/** Ant Design Vue pagination event payload */
export type AntPagination = { current: number; pageSize: number }

/** Type-safe cast for a-table's `record` in bodyCell template slots */
export function typed<T>(record: unknown): T {
  return record as T
}
