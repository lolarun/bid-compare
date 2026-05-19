/**
 * Runtime guards for extraction-job result shapes.
 *
 * AUDIT-FIX M9: pages currently cast `job.result` directly to typed shapes.
 * If the backend contract drifts, you get `undefined` silently. These
 * guards do best-effort coercion and warn loudly on shape mismatch.
 */

import type {
  ExtractionJob,
  QuoteExtractionItem,
  TenderExtractionItem,
} from '@/api/client'

export interface TenderExtractionShape {
  project_name: string
  project_code: string
  tender_date: string
  deadline: string
  items: TenderExtractionItem[]
}

export interface QuoteExtractionShape {
  supplier_name: string
  quote_date: string
  items: QuoteExtractionItem[]
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function asStr(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function asNumOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/** Coerce arbitrary tender-result JSON into TenderExtractionShape. */
export function asTenderShape(result: unknown): TenderExtractionShape {
  if (!isObj(result)) {
    console.warn('[extraction] tender result is not an object', result)
    return { project_name: '', project_code: '', tender_date: '', deadline: '', items: [] }
  }
  const rawItems = result.items
  const items: TenderExtractionItem[] = Array.isArray(rawItems)
    ? rawItems.filter(isObj).map((it) => ({
        name: asStr(it.name),
        category: asStr(it.category),
        spec: asStr(it.spec),
        unit: asStr(it.unit),
        quantity: asNumOrNull(it.quantity),
        remark: asStr(it.remark),
      }))
    : []
  if (!Array.isArray(rawItems)) {
    console.warn('[extraction] tender result.items is not an array', rawItems)
  }
  return {
    project_name: asStr(result.project_name),
    project_code: asStr(result.project_code),
    tender_date: asStr(result.tender_date),
    deadline: asStr(result.deadline),
    items,
  }
}

/** Coerce arbitrary quote-result JSON into QuoteExtractionShape. */
export function asQuoteShape(result: unknown): QuoteExtractionShape {
  if (!isObj(result)) {
    console.warn('[extraction] quote result is not an object', result)
    return { supplier_name: '', quote_date: '', items: [] }
  }
  const rawItems = result.items
  const items: QuoteExtractionItem[] = Array.isArray(rawItems)
    ? rawItems.filter(isObj).map((it) => ({
        material: asStr(it.material),
        spec: asStr(it.spec),
        brand: asStr(it.brand),
        unit: asStr(it.unit),
        qty: asNumOrNull(it.qty),
        unit_price: asNumOrNull(it.unit_price),
        unit_price_excl_tax: asNumOrNull(it.unit_price_excl_tax),
        total_price: asNumOrNull(it.total_price),
        tax_rate: asNumOrNull(it.tax_rate),
        remark: asStr(it.remark),
      }))
    : []
  if (!Array.isArray(rawItems)) {
    console.warn('[extraction] quote result.items is not an array', rawItems)
  }
  return {
    supplier_name: asStr(result.supplier_name),
    quote_date: asStr(result.quote_date),
    items,
  }
}

/** Convenience: dispatch on the job type. */
export function asExtractionShape(
  job: ExtractionJob,
): TenderExtractionShape | QuoteExtractionShape {
  return job.type === 'tender' ? asTenderShape(job.result) : asQuoteShape(job.result)
}
