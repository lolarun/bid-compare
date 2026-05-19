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
