import { describe, it, expect } from 'vitest'
import { translateReason, qualityTierCopy, issueMeta } from '../utils/doubtCopy'

describe('doubtCopy', () => {
  describe('translateReason', () => {
    it('translates declared_total_diff with formatted money', () => {
      const r = translateReason('declared_total_diff=5116.34')
      expect(r).toContain('¥5,116.34')
      expect(r).not.toContain('declared_total_diff')
    })

    it('translates no_seq_rows', () => {
      expect(translateReason('no_seq_rows=137')).toContain('137 行没有原文序号')
    })

    it('translates bbox_coverage=0', () => {
      expect(translateReason('bbox_coverage=0 (no row-level localization)')).toContain('像素坐标')
    })

    it('translates orientation_unresolved_pages', () => {
      const r = translateReason('orientation_unresolved_pages=[3, 7]')
      expect(r).toContain('[3, 7]')
      expect(r).toContain('方向')
    })

    it('translates no_price_column_mapped prefix regardless of suffix', () => {
      const r = translateReason(
        'no_price_column_mapped; header=[单位,单价]; unmapped_numeric_columns=[单价]'
      )
      expect(r).toContain('合价')
    })

    it('translates row_conservation_unverifiable and keeps the Chinese reason', () => {
      const r = translateReason('row_conservation_unverifiable: 序号覆盖率 0% < 80%，无序号轴可用')
      expect(r).toContain('序号覆盖率 0% < 80%')
    })

    it('passes through already-Chinese sequence reasons unchanged', () => {
      const raw = '序号缺口 3/40（7.5%）：[12, 27, 31]'
      expect(translateReason(raw)).toBe(raw)
    })

    it('passes through unknown signals unchanged rather than hiding them', () => {
      const raw = 'some_future_signal=42'
      expect(translateReason(raw)).toBe(raw)
    })
  })

  describe('qualityTierCopy', () => {
    it('maps BLOCKED to error tone', () => {
      expect(qualityTierCopy('BLOCKED')).toEqual({ label: '有问题，暂不能入库', tone: 'error' })
    })
    it('maps REVIEW to warning tone', () => {
      expect(qualityTierCopy('REVIEW').tone).toBe('warning')
    })
    it('maps PASS/undefined to success tone', () => {
      expect(qualityTierCopy('PASS').tone).toBe('success')
      expect(qualityTierCopy(undefined).tone).toBe('success')
    })
  })

  describe('issueMeta', () => {
    it('marks all four B3 dry-run error codes as block severity', () => {
      for (const code of [
        'structural_integrity_requires_review',
        'missing_total_requires_review',
        'all_rows_skipped',
        'declared_total_mismatch',
      ]) {
        expect(issueMeta(code).severity).toBe('block')
      }
    })
    it('falls back to review severity for unknown codes', () => {
      expect(issueMeta('some_new_code').severity).toBe('review')
    })
  })
})
