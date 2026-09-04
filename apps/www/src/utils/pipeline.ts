/**
 * 品类流水线的展示映射（2026-09-03，项目概述页重排）。
 *
 * **这是一张纯展示映射表，不是第二套业务判断。** 当前停在第几步完全由后端的
 * `next_action.code` 决定；前端不得再看"有没有清单、有几份报价"另算一遍
 * ——CLAUDE.md §4：`next_action` 由后端 `derive_next_action` 唯一裁定，前端
 * 自算必然随时间和后端漂移，而漂移的方向恰好是"看起来更完成"。
 *
 * 抽成独立模块只为一件事：这张表错一格，页面就会对项目状态说谎，而它写在
 * SFC 里没法单测。
 */
import type { NextActionCode } from '@/api/client'

/** 五步是为了流程可读；映射表仍然只有五个 code（见下）。 */
export const PIPELINE_STEPS = [
  '上传识别',
  '确认采购清单',
  '报价入库',
  '校对确认',
  '定标基准',
] as const

/**
 * `next_action.code` → Steps 停在第几步（0 基）。
 *
 * 「报价入库」（index 2）没有自己的 code：报价一旦入库就直接进入
 * `pending_intake`（待校对）或 `ready_to_compare`，所以它永远不是"当前步"，
 * 只会以"已完成"的身份出现。这不是漏了一个 code，是流程本身没有停在那里的
 * 状态。
 *
 * `basis_set` 映射到 `PIPELINE_STEPS.length`（越过末步）= 五步全部完成。
 */
const NEXT_ACTION_STEP: Record<NextActionCode, number> = {
  pending_upload: 0,
  list_unconfirmed: 1,
  pending_intake: 3,
  ready_to_compare: 4,
  basis_set: PIPELINE_STEPS.length,
}

/** 未知 code 回落到第 0 步：后端将来加了新 code 而前端还没跟上时，宁可显示
 *  "才刚开始"，也不要显示"已完成"——后者会让人以为可以定标了。 */
export function pipelineStep(code: NextActionCode): number {
  return NEXT_ACTION_STEP[code] ?? 0
}
