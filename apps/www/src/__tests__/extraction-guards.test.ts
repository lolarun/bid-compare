import { describe, it, expect, vi } from 'vitest'
import {
  asQuoteShape,
  asTenderShape,
  asExtractionShape,
} from '../utils/extraction'

describe('extraction runtime guards', () => {
  describe('asTenderShape', () => {
    it('produces empty shape for null', () => {
      const r = asTenderShape(null)
      expect(r.project_name).toBe('')
      expect(r.items).toEqual([])
    })

    it('produces empty shape for non-object', () => {
      const r = asTenderShape('garbage')
      expect(r.items).toEqual([])
    })

    it('coerces a well-formed result', () => {
      const r = asTenderShape({
        project_name: 'P',
        project_code: 'C',
        tender_date: '2026-05-20',
        deadline: '2026-06-15',
        items: [{ name: '桥架', category: '桥架', spec: '300', quantity: 100 }],
      })
      expect(r.project_name).toBe('P')
      expect(r.items.length).toBe(1)
      expect(r.items[0].name).toBe('桥架')
      expect(r.items[0].quantity).toBe(100)
    })

    it('drops non-dict items but warns', () => {
      const spy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const r = asTenderShape({
        project_name: 'P',
        items: [{ name: 'A' }, 'string', null, { name: 'B' }],
      })
      expect(r.items.length).toBe(2)
      expect(r.items.map((i) => i.name)).toEqual(['A', 'B'])
      spy.mockRestore()
    })

    it('warns when items is not an array', () => {
      const spy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const r = asTenderShape({ project_name: 'P', items: 'not-array' })
      expect(spy).toHaveBeenCalled()
      expect(r.items).toEqual([])
      spy.mockRestore()
    })

    it('coerces string numerics to null when invalid', () => {
      const r = asTenderShape({
        items: [{ name: 'X', quantity: 'not a number' }],
      })
      expect(r.items[0].quantity).toBeNull()
    })
  })

  describe('asQuoteShape', () => {
    it('handles missing fields gracefully', () => {
      const r = asQuoteShape({ items: [{ material: 'M' }] })
      expect(r.items[0].material).toBe('M')
      expect(r.items[0].unit_price).toBeNull()
      expect(r.items[0].qty).toBeNull()
    })

    it('coerces numeric strings to numbers', () => {
      const r = asQuoteShape({ items: [{ material: 'M', qty: '12', unit_price: '99.5' }] })
      expect(r.items[0].qty).toBe(12)
      expect(r.items[0].unit_price).toBe(99.5)
    })

    it('keeps strings as-is for text fields', () => {
      const r = asQuoteShape({
        items: [{ material: 'M', brand: '良工', remark: '5 年保修' }],
      })
      expect(r.items[0].brand).toBe('良工')
      expect(r.items[0].remark).toBe('5 年保修')
    })

    it('preserves price-basis bridge fields end-to-end (no silent drop)', () => {
      // 模拟后端 _draft_to_quote_response → _postprocess_quote 输出的完整 item（凯硕双口径）
      const backendItem = {
        material: 'Y型过滤器', spec: 'DN50', brand: '凯硕', unit: '个', qty: 4,
        unit_price: null, unit_price_incl_tax: 865, unit_price_excl_tax: 765.49,
        total_price: null, total_price_incl_tax: 3460, total_price_excl_tax: 3061.95,
        tax_rate: 0.13, tax_amount: 398.05,
        price_basis: 'dual_tax', effective_unit_price: 865, effective_total_price: 3460,
        validation_flags: ['qty_arithmetic_mismatch'], raw_qty: 1, suggested_qty: 4,
        canonical: { dn: 50 }, source_ref: { page: 7, table: 0, row: 88 },
      }
      const r = asQuoteShape({ supplier_name: '凯硕', items: [backendItem] })
      const it = r.items[0]
      // 所有桥接字段必须存活且数值化
      expect(it.unit_price_incl_tax).toBe(865)
      expect(it.unit_price_excl_tax).toBe(765.49)
      expect(it.total_price_incl_tax).toBe(3460)
      expect(it.total_price_excl_tax).toBe(3061.95)
      expect(it.tax_amount).toBe(398.05)
      expect(it.price_basis).toBe('dual_tax')
      expect(it.effective_unit_price).toBe(865)
      expect(it.effective_total_price).toBe(3460)
      expect(it.validation_flags).toEqual(['qty_arithmetic_mismatch'])
      expect(it.raw_qty).toBe(1)
      expect(it.suggested_qty).toBe(4)
      // 隐藏字段照旧存活
      expect(it.canonical).toEqual({ dn: 50 })
      expect(it.source_ref).toEqual({ page: 7, table: 0, row: 88 })
    })

    it('coerces numeric-string bridge fields and unspecified basis (绵存)', () => {
      const r = asQuoteShape({
        items: [{
          material: '法兰', qty: '1', unit_price: '93', total_price: '93',
          price_basis: 'unspecified', effective_unit_price: '93', effective_total_price: '93',
        }],
      })
      const it = r.items[0]
      expect(it.price_basis).toBe('unspecified')
      expect(it.effective_unit_price).toBe(93)
      expect(it.effective_total_price).toBe(93)
      // 不含税/含税字段缺失 → null，不得臆造
      expect(it.unit_price_incl_tax).toBeNull()
      expect(it.total_price_excl_tax).toBeNull()
    })
  })

  describe('asExtractionShape', () => {
    it('dispatches on job.type', () => {
      const t = asExtractionShape({
        id: '1', type: 'tender', status: 'done', filename: 'a',
        file_size: 0, context: {}, result: { project_name: 'P', items: [] },
        error: '', confidence: 1, provider: '', tokens_used: 0,
        duration_ms: 0, created_at: null, updated_at: null,
      })
      expect('project_name' in t).toBe(true)

      const q = asExtractionShape({
        id: '2', type: 'quote', status: 'done', filename: 'b',
        file_size: 0, context: {}, result: { items: [] },
        error: '', confidence: 1, provider: '', tokens_used: 0,
        duration_ms: 0, created_at: null, updated_at: null,
      })
      expect('supplier_name' in q).toBe(true)
    })
  })
})
