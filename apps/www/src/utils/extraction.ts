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
  TenderBrandReq,
  TenderSupplierBrand,
  QualityMeta,
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
  /**
   * 报价行投票出的品类（后端 2026-08-23 新增，PDF/Excel 两条路同一函数产出）。
   *
   * 没有采购清单时这是品类的**唯一**来源：招标文件不带清单（实测有招标 PDF
   * 的材料明细表整行写"详见附件1"、附件未装订）时，`category` 恒为空串，
   * 每一份报价都会被 `batch-confirm` 拒收，而界面上没有手动选品类的控件——
   * 用户到这一步是死路。把握不足时后端返回空串，交人工选择，不猜。
   */
  detected_category: string
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
    return { supplier_name: '', quote_date: '', items: [], detected_category: '' }
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
        // design/24 B0：副本编号必须往返，否则后端拿到的 items 里 copy_no
        // 全部丢失，重复副本判别只能靠"内容重复"去猜，等于没有这个字段。
        if (typeof rest.copy_no === 'string') hidden.copy_no = rest.copy_no
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
    detected_category: asStr(result.detected_category),
  }
}

export interface TenderBidlistShape {
  items: TenderExtractionItem[]
  brandRequirements: string[]   // 业主品牌要求（招标文件第13页，PDF 主清单要求）
  // R4：supplierBrands 是"各投标单位参与品牌"——与 brandRequirements（业主
  // 品牌要求）是后端提示词专门区分的两个概念（防混淆），此前前端只取用了
  // brandRequirements，supplier_brands 从未从这个 shape 里暴露过。
  supplierBrands: TenderSupplierBrand[]
  projectName: string
  projectCode: string
  tenderDate: string
  deadline: string
}

/**
 * Coerce TenderBidlistResult (type=tender_bidlist, TenderAnchor format) into
 * the invite flow's working types:
 *   items            → TenderExtractionItem[] (compatible with ExtractionEditor schema="tender")
 *   brandRequirements → string[] (brand_cn || brand_en, deduplicated)
 *
 * TenderAnchor fields used: name, spec, unit, qty → quantity, remark, category.
 * `category` is passed through when the pipeline set it; infer_categories on the
 * backend falls back to keyword matching when the field is absent or empty.
 */
export function asTenderBidlistShape(result: unknown): TenderBidlistShape {
  if (!isObj(result)) {
    console.warn('[extraction] tender_bidlist result is not an object', result)
    return {
      items: [], brandRequirements: [], supplierBrands: [],
      projectName: '', projectCode: '', tenderDate: '', deadline: '',
    }
  }

  const rawItems = result.items
  const items: TenderExtractionItem[] = Array.isArray(rawItems)
    ? rawItems.filter(isObj).map((it) => ({
        name: asStr(it.name),
        category: asStr(it.category),
        spec: asStr(it.spec),
        unit: asStr(it.unit),
        quantity: asNumOrNull(it.qty ?? it.quantity),
        remark: asStr(it.remark),
        extended_attrs: {},
      }))
    : []

  if (!Array.isArray(rawItems)) {
    console.warn('[extraction] tender_bidlist result.items is not an array', rawItems)
  }

  const rawBrands = result.brand_requirement
  const brandRequirements: string[] = []
  const seen = new Set<string>()
  if (Array.isArray(rawBrands)) {
    for (const b of rawBrands) {
      if (!isObj(b)) continue
      const label = asStr((b as unknown as TenderBrandReq).brand_cn) || asStr((b as unknown as TenderBrandReq).brand_en)
      if (label && !seen.has(label)) {
        brandRequirements.push(label)
        seen.add(label)
      }
    }
  }

  const rawSupplierBrands = result.supplier_brands
  const supplierBrands: TenderSupplierBrand[] = Array.isArray(rawSupplierBrands)
    ? rawSupplierBrands.filter(isObj).map((sb) => ({
        supplier_name: asStr(sb.supplier_name),
        brand: asStr(sb.brand),
        supplier_id: typeof sb.supplier_id === 'number' ? sb.supplier_id : null,
      }))
    : []

  return {
    items,
    brandRequirements,
    supplierBrands,
    projectName: asStr(result.project_name),
    projectCode: asStr(result.project_code),
    tenderDate: asStr(result.tender_date),
    deadline: asStr(result.deadline),
  }
}

/** Convenience: dispatch on the job type. */
export function asExtractionShape(
  job: ExtractionJob,
): TenderExtractionShape | QuoteExtractionShape {
  return job.type === 'tender' ? asTenderShape(job.result) : asQuoteShape(job.result)
}

/**
 * Extract the `_quality` metadata block (评审 R2) from a job result.
 * Returns null when absent — older jobs recognized before the backend fix
 * (or jobs whose pipeline never set it) simply have no signal to show;
 * callers must treat null as "unknown", not as "PASS".
 */
export function asQualityMeta(result: unknown): QualityMeta | null {
  if (!isObj(result)) return null
  const q = result._quality
  return isObj(q) ? (q as unknown as QualityMeta) : null
}

/**
 * design/29 §10 req5：文件自己声明的投标总价（封面/汇总页抽出来的
 * `_doc_meta.bid_total`），跟明细逐行相加的合计是**两个事实**——卡片上
 * 分别陈述，不合并。拿不到时返回 null（"读不到"不等于"没有"），调用方
 * 按缺失处理，不得用明细合计冒充。
 */
export function asDeclaredTotal(result: unknown): number | null {
  if (!isObj(result)) return null
  const dm = result._doc_meta
  return isObj(dm) ? asNumOrNull(dm.bid_total) : null
}
