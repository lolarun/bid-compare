/**
 * design/24 §5：疑点收件箱的人话文案表。
 *
 * 后端两类信号分别处理：
 * 1. `QualityMeta.quality_blocking_reasons`（extraction_draft.py::compute_quality +
 *    vl_quote.py 追加的方向/序号/价格列三条）—— 大多是 `key=value` 形式的原始诊断
 *    字符串，给工程师看的，不是给用户看的（用户反馈 #5："识别质量提示没有啥意义，
 *    对于客户来说太晦涩了"）。translateReason() 按前缀模式翻译成人话；序号相关的
 *    reason（check_sequence_continuity 产出）本来就是中文，原样透传即可。
 * 2. `BatchConfirmIssue.error`（quote_confirmation_service.py 的四道数据质量门，
 *    dry_run=true 时收集而不阻断）—— message 字段后端已经写好人话了，这里只需要
 *    把 error 码映射到严重度和收件箱里的短标签，不重复翻译一遍。
 *
 * 任何未覆盖到的新信号：宁可原样显示原始字符串，也不能吞掉信息——这是识别链路
 * 的诊断证据，藏起来等于制造"看起来没问题"的假象。
 */

export interface TranslatedRule {
  test: RegExp
  render: (m: RegExpMatchArray) => string
}

function fmtMoney(n: number): string {
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// 顺序即优先级：越靠前的模式越先匹配。
const RULES: TranslatedRule[] = [
  {
    test: /^failed_target_pages=(\[.*\])$/,
    render: (m) => `有几页在识别时报错、没能处理完整（页码 ${m[1]}），需要重新识别或人工核对这几页。`,
  },
  {
    test: /^document_truncated$/,
    render: () => '文件在处理中被截断，不是完整识别结果，请重新上传或重新识别。',
  },
  {
    test: /^zero_quote_lines_with_data$/,
    render: () => '页面里有内容，但一行报价明细都没能识别出来，这份文件可能需要人工核对或换个版式重新识别。',
  },
  {
    test: /^declared_total_diff=([\d.]+)$/,
    render: (m) => `识别出的合价合计与文件里写的声明总价对不上（差 ¥${fmtMoney(Number(m[1]))}），可能有些行漏读或读错了金额。`,
  },
  {
    test: /^under_extracted_pages=(\d+)\/(\d+)$/,
    render: (m) => `有 ${m[1]}/${m[2]} 页识别出的行数明显少于预期，这些页可能有漏行，建议核对原文。`,
  },
  {
    test: /^under_extracted_pages=(\[.*\])$/,
    render: (m) => `第 ${m[1]} 页识别出的行数偏少，可能有漏行，建议核对原文这几页。`,
  },
  {
    test: /^qty_arithmetic_mismatch_blocked=(\d+)$/,
    render: (m) => `有 ${m[1]} 行"数量 × 单价 ≠ 合价"且缺少可信证据支持，系统不会代为纠正，需要人工核对这些行。`,
  },
  {
    test: /^qty_arithmetic_mismatch=(\d+)$/,
    render: (m) => `有 ${m[1]} 行"数量 × 单价 ≠ 合价"，已标记但不阻断入库，建议留意核对。`,
  },
  {
    test: /^arithmetic_consistency=([\d.]+)$/,
    render: (m) => `逐行金额的算术自洽率只有 ${Math.round(Number(m[1]) * 100)}%，建议核对几个明显对不上的行。`,
  },
  {
    test: /^tax_basis_inconsistent$/,
    render: () => '同一份文件里含税/不含税口径混用了，建议确认最终按哪个口径入库。',
  },
  {
    test: /^source_ref_coverage=([\d.]+)$/,
    render: (m) => `只有 ${Math.round(Number(m[1]) * 100)}% 的行能定位回原文，其余行的来源暂时无法追溯。`,
  },
  {
    test: /^seq_missing=(\[.*\])$/,
    render: (m) => `原文序号里缺了这些号：${m[1]}，可能是漏识别，也可能原文本身跳号，建议核对。`,
  },
  {
    test: /^bbox_coverage=0.*$/,
    render: () => '这份文件没有逐行的像素坐标，暂时无法在原文里高亮定位到具体行。',
  },
  {
    test: /^no_seq_rows=(\d+)$/,
    render: (m) => `有 ${m[1]} 行没有原文序号，无法用序号核对身份，建议人工确认这些行对应原文的哪一行。`,
  },
  {
    test: /^orientation_unresolved_pages=(\[.*\])$/,
    render: (m) => `有几页（页码 ${m[1]}）的文字方向没能自动判断（可能是扫描件倾斜），建议人工检查这几页有没有被读对。`,
  },
  {
    test: /^no_price_column_mapped;.*$/,
    render: () => '没能在表格里找到"合价"这一列，这份文件的价格可能完全没识别到，建议人工核对或换个版式重新识别。',
  },
  {
    test: /^row_conservation_unverifiable:\s*(.+)$/,
    render: (m) => `无法自动验证是否漏行（${m[1]}），建议按原文页数或目录粗略核对一下总行数。`,
  },
]

/** 把一条原始诊断字符串翻成人话；没有匹配规则的原样返回（不吞信息）。*/
export function translateReason(raw: string): string {
  for (const rule of RULES) {
    const m = raw.match(rule.test)
    if (m) return rule.render(m)
  }
  return raw
}

export type DoubtSeverity = 'block' | 'review'

export interface QualityTierCopy {
  label: string
  tone: 'error' | 'warning' | 'success'
}

/** quality_status（PASS/REVIEW/BLOCKED）→ 横幅人话，不再直接展示英文档位词。*/
export function qualityTierCopy(status: string | undefined | null): QualityTierCopy {
  if (status === 'BLOCKED') return { label: '有问题，暂不能入库', tone: 'error' }
  if (status === 'REVIEW') return { label: '建议人工核对后再入库', tone: 'warning' }
  return { label: '识别质量正常', tone: 'success' }
}

export interface IssueMeta {
  severity: DoubtSeverity
  shortLabel: string
}

// design/24 B3：dry_run 四道门的 error 码 → 收件箱短标签 + 严重度。message 本身
// 后端已经是人话，这里不重复翻译，只做分类。
const ISSUE_META: Record<string, IssueMeta> = {
  structural_integrity_requires_review: { severity: 'block', shortLabel: '结构完整性未过' },
  missing_total_requires_review: { severity: 'block', shortLabel: '原文无合价' },
  all_rows_skipped: { severity: 'block', shortLabel: '整份未入库' },
  declared_total_mismatch: { severity: 'block', shortLabel: '总价对不上' },
}

export function issueMeta(errorCode: string): IssueMeta {
  return ISSUE_META[errorCode] ?? { severity: 'review', shortLabel: '待核对' }
}
