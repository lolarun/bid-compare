/**
 * design/24 §2/§5：疑点收件箱。把散落在多处的"人需要做判断"的信号——识别质量
 * 提示、批量校对时的四道数据质量门、招标清单对账差异、锚点对齐待处理——收拢成
 * 一份统一列表。用户反馈 #6/#7/#8："总是提示我一行不对，我去哪里调整？我建议
 * 重新设计一下流程"——收件箱本身不发明新判据，只是把已有判据翻成人话、给一个
 * 跳转口子，判据的产生位置和阻断逻辑完全不变（CLAUDE.md §4 质量分级不可下移）。
 *
 * 刻意不做的事：不在这里主动 watch batchFiles 深层变化去自动重跑 dry-run——
 * ExtractionEditor 里编辑一个字段就会触发一次请求会把后端打爆。dry-run 的刷新
 * 由调用方在合适的时机（进入 Inbox 页 / 编辑保存后 / 手动"重新核对"）显式触发。
 */
import { computed, reactive, type Ref } from 'vue'
import { quoteApi } from '@/api'
import type { BatchConfirmResult } from '@/api/client'
import type { BatchFileEntry, UploadTaskConfig } from '@/composables/useSupplierUpload'
import { translateReason, issueMeta, type DoubtSeverity } from '@/utils/doubtCopy'

export type DoubtKind = 'quality' | 'structural' | 'reconcile' | 'alignment'

export interface DoubtItem {
  id: string
  kind: DoubtKind
  severity: DoubtSeverity
  sourceLabel: string   // 哪份文件/哪个环节，用于列表展示
  title: string
  detail?: string
  actionLabel: string
  action: () => void
}

export interface DoubtInboxDeps {
  batchFiles: Ref<BatchFileEntry[]>
  taskConfig: UploadTaskConfig
  reconcileResult: Ref<{
    only_in_excel_reference?: string[]
    field_mismatches: unknown[]
    recommended_source: string
  } | null>
  reconcileConfirmed: Ref<boolean>
  anchorReviewResult: Ref<{ pending_items_total?: number; low_conf_groups: unknown[] } | null>
  // 跳转口子——由 IndexView/Stage 组件按各自的路由与滚动逻辑实现，本 composable 不碰 DOM/路由。
  onGoToFile: (fileId: string) => void
  onGoToReconcile: () => void
  onGoToAlignment: () => void
}

export function useDoubtInbox(deps: DoubtInboxDeps) {
  const {
    batchFiles, taskConfig, reconcileResult, reconcileConfirmed,
    anchorReviewResult, onGoToFile, onGoToReconcile, onGoToAlignment,
  } = deps

  // fileId -> 最近一次 dry-run 结果；只在显式调用 refreshDryRun(s) 时更新。
  const dryRunByFile = reactive<Record<string, BatchConfirmResult>>({})
  const dryRunLoading = reactive<Record<string, boolean>>({})

  function eligibleForDryRun(f: BatchFileEntry): boolean {
    // 只有识别完成、还没入库、名称已经填了的文件才值得预检——跟真实入库门的前置条件一致，
    // 否则会拿一堆必然失败的半成品去问后端，制造噪音而不是疑点。
    return f.status === 'done' && !f.confirmed && !!f.finalSupplierName.trim() && !!f.jobId
  }

  async function refreshDryRun(f: BatchFileEntry): Promise<void> {
    if (!eligibleForDryRun(f)) return
    dryRunLoading[f.id] = true
    try {
      const supplierId = f.matchedSupplierId ?? undefined
      const { data } = await quoteApi.batchConfirm({
        job_id: f.jobId as string,
        supplier_id: supplierId,
        supplier_name: f.finalSupplierName.trim(),
        project_id: taskConfig.projectId,
        category: taskConfig.category,
        overrides: f.items as unknown as Array<Record<string, unknown>>,
        bid_status: taskConfig.bidStatus,
        dry_run: true,
      })
      dryRunByFile[f.id] = data
    } catch {
      // 预检本身失败（网络/鉴权等）不构成疑点，静默丢弃这次结果；不影响真正入库时的门。
      delete dryRunByFile[f.id]
    } finally {
      dryRunLoading[f.id] = false
    }
  }

  async function refreshAllDryRuns(): Promise<void> {
    for (const f of batchFiles.value) {
      if (eligibleForDryRun(f)) await refreshDryRun(f)
    }
  }

  const items = computed<DoubtItem[]>(() => {
    const out: DoubtItem[] = []

    for (const f of batchFiles.value) {
      if (f.confirmed) continue   // 已经入库的文件不再占收件箱位置

      // — 识别质量信号（compute_quality + vl_quote.py 追加的方向/序号/价格列）—
      const q = f.quality
      if (q && q.quality_status && q.quality_status !== 'PASS' && q.quality_blocking_reasons?.length) {
        const reasons = q.quality_blocking_reasons.map(translateReason)
        out.push({
          id: `quality:${f.id}`,
          kind: 'quality',
          severity: q.quality_status === 'BLOCKED' ? 'block' : 'review',
          sourceLabel: f.finalSupplierName || f.filename,
          title: q.quality_status === 'BLOCKED'
            ? `${f.finalSupplierName || f.filename}：识别有问题，暂不能入库`
            : `${f.finalSupplierName || f.filename}：建议核对后再入库`,
          detail: reasons.join('；'),
          actionLabel: '去查看',
          action: () => onGoToFile(f.id),
        })
      }

      // — 批量校对四道数据质量门（dry_run 预检结果）—
      const dr = dryRunByFile[f.id]
      if (dr?.issues?.length) {
        const worst: DoubtSeverity = dr.issues.some((i) => issueMeta(i.error).severity === 'block')
          ? 'block' : 'review'
        out.push({
          id: `structural:${f.id}`,
          kind: 'structural',
          severity: worst,
          sourceLabel: f.finalSupplierName || f.filename,
          title: `${f.finalSupplierName || f.filename}：${dr.issues.map((i) => issueMeta(i.error).shortLabel).join('、')}`,
          detail: dr.issues.map((i) => i.message).join('\n'),
          actionLabel: '去核对',
          action: () => onGoToFile(f.id),
        })
      }
    }

    // — 招标清单对账差异 —
    const rc = reconcileResult.value
    if (rc && !reconcileConfirmed.value && rc.recommended_source === 'excel') {
      const excelOnly = rc.only_in_excel_reference?.length ?? 0
      const mismatches = rc.field_mismatches.length
      if (excelOnly > 0 || mismatches > 0) {
        out.push({
          id: 'reconcile',
          kind: 'reconcile',
          severity: 'review',
          sourceLabel: '采购清单对账',
          title: `采购清单对账有差异：${excelOnly > 0 ? `Excel 独有 ${excelOnly} 行` : ''}${excelOnly > 0 && mismatches > 0 ? '，' : ''}${mismatches > 0 ? `字段不一致 ${mismatches} 处` : ''}`,
          actionLabel: '去核对',
          action: onGoToReconcile,
        })
      }
    }

    // — 锚点对齐待处理 —
    const ar = anchorReviewResult.value
    if (ar) {
      const pending = ar.pending_items_total ?? ar.low_conf_groups.length
      if (pending > 0) {
        out.push({
          id: 'alignment',
          kind: 'alignment',
          severity: 'review',
          sourceLabel: '对齐结果',
          title: `有 ${pending} 处对齐结果需要人工确认`,
          actionLabel: '去确认',
          action: onGoToAlignment,
        })
      }
    }

    return out
  })

  const blockingCount = computed(() => items.value.filter((i) => i.severity === 'block').length)
  const reviewCount = computed(() => items.value.filter((i) => i.severity === 'review').length)
  const isEmpty = computed(() => items.value.length === 0)

  return {
    items, blockingCount, reviewCount, isEmpty,
    dryRunByFile, dryRunLoading,
    refreshDryRun, refreshAllDryRuns,
  }
}
