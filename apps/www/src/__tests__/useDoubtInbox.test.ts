import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'
import { useDoubtInbox } from '../composables/useDoubtInbox'
import type { BatchFileEntry, UploadTaskConfig } from '../composables/useSupplierUpload'

vi.mock('@/api', () => ({
  quoteApi: { batchConfirm: vi.fn() },
}))

import { quoteApi } from '@/api'

function makeFile(overrides: Partial<BatchFileEntry> = {}): BatchFileEntry {
  return {
    id: 'f1',
    filename: 'a.pdf',
    status: 'done',
    stage: 'done',
    stageDetail: '',
    progressPct: 100,
    uploadPct: 100,
    jobId: 'job-1',
    detectedSupplierName: '甲供应商',
    finalSupplierName: '甲供应商',
    matchedSupplierId: null,
    nameConflictHints: [],
    items: [],
    quality: null,
    confirmedSupplierId: null,
    confirmedSubmissionId: null,
    confirmed: false,
    confirming: false,
    error: '',
    pollTimer: null,
    ...overrides,
  }
}

const taskConfig: UploadTaskConfig = {
  projectId: 1,
  category: '电缆',
  supplierIds: [],
  bidStatus: 'submitted',
}

describe('useDoubtInbox', () => {
  beforeEach(() => {
    vi.mocked(quoteApi.batchConfirm).mockReset()
  })

  it('produces a quality item for a non-PASS confirmed-false file, translated to plain language', () => {
    const batchFiles = ref<BatchFileEntry[]>([
      makeFile({
        quality: {
          quality_status: 'BLOCKED',
          quality_blocking_reasons: ['no_price_column_mapped; header=[x]; unmapped_numeric_columns=[x]'],
        },
      }),
    ])
    const inbox = useDoubtInbox({
      batchFiles,
      taskConfig,
      reconcileResult: ref(null),
      reconcileConfirmed: ref(false),
      anchorReviewResult: ref(null),
      onGoToFile: vi.fn(),
      onGoToReconcile: vi.fn(),
      onGoToAlignment: vi.fn(),
    })
    expect(inbox.items.value).toHaveLength(1)
    expect(inbox.items.value[0].severity).toBe('block')
    expect(inbox.items.value[0].detail).toContain('合价')
    expect(inbox.blockingCount.value).toBe(1)
  })

  it('skips confirmed files entirely', () => {
    const batchFiles = ref<BatchFileEntry[]>([
      makeFile({
        confirmed: true,
        quality: { quality_status: 'BLOCKED', quality_blocking_reasons: ['document_truncated'] },
      }),
    ])
    const inbox = useDoubtInbox({
      batchFiles, taskConfig,
      reconcileResult: ref(null), reconcileConfirmed: ref(false),
      anchorReviewResult: ref(null),
      onGoToFile: vi.fn(), onGoToReconcile: vi.fn(), onGoToAlignment: vi.fn(),
    })
    expect(inbox.isEmpty.value).toBe(true)
  })

  it('refreshDryRun calls batchConfirm with dry_run:true and populates structural items', async () => {
    vi.mocked(quoteApi.batchConfirm).mockResolvedValue({
      data: {
        status: 'ok', submission_id: 0, line_count: 0, skipped_count: 0, errors: [],
        unknown_brands: [], supplier_id: null, project_id: 1, batch_id: 'b1',
        dry_run: true, would_succeed: false,
        issues: [{ error: 'declared_total_mismatch', message: '合价合计与声明总价对不上，超过允许范围。' }],
      },
    } as never)
    const batchFiles = ref<BatchFileEntry[]>([makeFile()])
    const inbox = useDoubtInbox({
      batchFiles, taskConfig,
      reconcileResult: ref(null), reconcileConfirmed: ref(false),
      anchorReviewResult: ref(null),
      onGoToFile: vi.fn(), onGoToReconcile: vi.fn(), onGoToAlignment: vi.fn(),
    })
    await inbox.refreshDryRun(batchFiles.value[0])
    expect(quoteApi.batchConfirm).toHaveBeenCalledWith(
      expect.objectContaining({ dry_run: true, job_id: 'job-1' })
    )
    expect(inbox.items.value).toHaveLength(1)
    expect(inbox.items.value[0].kind).toBe('structural')
    expect(inbox.items.value[0].severity).toBe('block')
  })

  it('does not call batchConfirm for files not eligible (no supplier name yet)', async () => {
    const batchFiles = ref<BatchFileEntry[]>([makeFile({ finalSupplierName: '' })])
    const inbox = useDoubtInbox({
      batchFiles, taskConfig,
      reconcileResult: ref(null), reconcileConfirmed: ref(false),
      anchorReviewResult: ref(null),
      onGoToFile: vi.fn(), onGoToReconcile: vi.fn(), onGoToAlignment: vi.fn(),
    })
    await inbox.refreshAllDryRuns()
    expect(quoteApi.batchConfirm).not.toHaveBeenCalled()
  })

  it('surfaces a reconcile item only while unconfirmed and excel is recommended', () => {
    const reconcileResult = ref({
      only_in_excel_reference: ['12', '13'],
      field_mismatches: [],
      recommended_source: 'excel',
    })
    const goToReconcile = vi.fn()
    const inbox = useDoubtInbox({
      batchFiles: ref([]), taskConfig,
      reconcileResult, reconcileConfirmed: ref(false),
      anchorReviewResult: ref(null),
      onGoToFile: vi.fn(), onGoToReconcile: goToReconcile, onGoToAlignment: vi.fn(),
    })
    expect(inbox.items.value).toHaveLength(1)
    inbox.items.value[0].action()
    expect(goToReconcile).toHaveBeenCalled()
  })

  it('surfaces an alignment item when pending_items_total > 0', () => {
    const goToAlignment = vi.fn()
    const inbox = useDoubtInbox({
      batchFiles: ref([]), taskConfig,
      reconcileResult: ref(null), reconcileConfirmed: ref(false),
      anchorReviewResult: ref({ pending_items_total: 3, low_conf_groups: [] }),
      onGoToFile: vi.fn(), onGoToReconcile: vi.fn(), onGoToAlignment: goToAlignment,
    })
    expect(inbox.items.value).toHaveLength(1)
    expect(inbox.items.value[0].title).toContain('3')
    inbox.items.value[0].action()
    expect(goToAlignment).toHaveBeenCalled()
  })
})
