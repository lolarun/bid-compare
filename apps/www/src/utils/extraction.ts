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
        extended_attrs: isObj(it.extended_attrs) ? it.extended_attrs as Record<string, unknown> : {},
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

/** Coerce arbitrary quote-result JSON into QuoteExtractionShape.
 *
 * IMPORTANT: hidden fields (canonical, validation_warning, source_ref,
 * category, standard_name, standard_spec) are preserved on each item even
 * though they are never displayed in the ExtractionEditor UI.  They must
 * survive the round-trip to batch-confirm so that canonical reaches
 * anchor-match intact.  Do NOT add them to the explicit spread above —
 * they are passed through via the `...hidden` merge at the end of each item.
 */
export function asQuoteShape(result: unknown): QuoteExtractionShape {
  if (!isObj(result)) {
    console.warn('[extraction] quote result is not an object', result)
    return { supplier_name: '', quote_date: '', items: [] }
  }
  const rawItems = result.items
  const items: QuoteExtractionItem[] = Array.isArray(rawItems)
    ? rawItems.filter(isObj).map((it) => {
        // Destructure the hidden fields that must survive to batch-confirm.
        const {
          material, spec, brand, unit, qty, unit_price, unit_price_excl_tax,
          total_price, tax_rate, remark,
          // 价格口径桥接字段：显式解构并保留，绝不丢失（§4/§9）
          unit_price_incl_tax, total_price_incl_tax, total_price_excl_tax,
          tax_amount, price_basis, effective_unit_price, effective_total_price,
          validation_flags, raw_qty, suggested_qty, document_row_index,
          // visible fields handled explicitly above — everything else is hidden
          ...rest
        } = it as Record<string, unknown>
        // Pick only the known hidden keys (do not blindly pass rest to avoid
        // accidental large blobs; add keys here as the contract expands).
        const hidden: Partial<QuoteExtractionItem> = {}
        if (isObj(rest.canonical))          hidden.canonical          = rest.canonical as Record<string, unknown>
        if (typeof rest.validation_warning === 'string') hidden.validation_warning = rest.validation_warning
        if (isObj(rest.source_ref))         hidden.source_ref         = rest.source_ref as Record<string, unknown>
        if (typeof rest.material_type === 'string') hidden.material_type = rest.material_type
        if (typeof rest.normalized_material === 'string') hidden.normalized_material = rest.normalized_material
        if (typeof rest.ocr_correction_reason === 'string') hidden.ocr_correction_reason = rest.ocr_correction_reason
        if (typeof rest.category === 'string')      hidden.category      = rest.category
        if (typeof rest.standard_name === 'string') hidden.standard_name = rest.standard_name
        if (typeof rest.standard_spec === 'string') hidden.standard_spec = rest.standard_spec
        // 价格口径桥接字段：数值字段统一 asNumOrNull，basis 字符串，flags 数组
        const basis: Partial<QuoteExtractionItem> = {
          unit_price_incl_tax: asNumOrNull(unit_price_incl_tax),
          total_price_incl_tax: asNumOrNull(total_price_incl_tax),
          total_price_excl_tax: asNumOrNull(total_price_excl_tax),
          tax_amount: asNumOrNull(tax_amount),
          effective_unit_price: asNumOrNull(effective_unit_price),
          effective_total_price: asNumOrNull(effective_total_price),
          raw_qty: asNumOrNull(raw_qty),
          suggested_qty: asNumOrNull(suggested_qty),
          document_row_index: asNumOrNull(document_row_index),
        }
        if (typeof price_basis === 'string') basis.price_basis = price_basis
        if (Array.isArray(validation_flags)) {
          basis.validation_flags = validation_flags.filter((x): x is string => typeof x === 'string')
        }
        return {
          material: asStr(material),
          spec: asStr(spec),
          brand: asStr(brand),
          unit: asStr(unit),
          qty: asNumOrNull(qty),
          unit_price: asNumOrNull(unit_price),
          unit_price_excl_tax: asNumOrNull(unit_price_excl_tax),
          total_price: asNumOrNull(total_price),
          tax_rate: asNumOrNull(tax_rate),
          remark: asStr(remark),
          ...basis,
          ...hidden,
        }
      })
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
