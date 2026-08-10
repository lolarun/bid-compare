"""Centralised domain safety thresholds — single source of truth.

Tier classification (CLAUDE.md §7):
  SYSTEM  — resource limits: env-driven (OCR_RENDER_SCALE, PAGE_CONCURRENCY …)
  DOMAIN  — quality-gate thresholds defined here; change requires code review
  PROJECT — per-project evaluation rules stored in EvaluationPolicy / DB

All MATCH_* constants below are DOMAIN tier.
"""

# ── Match / quality-gate thresholds ─────────────────────────────────────────

# Min fraction of eligible rows that must have unit_price > 0
MATCH_PRICE_COVERAGE_THRESHOLD: float = 0.80

# Max fraction of evaluable rows allowed to have a hard arithmetic error
MATCH_ARITHMETIC_MAX_ERROR_RATE: float = 0.05

# Max fraction of eligible rows whose 合价 was derived (qty×unit_price) rather than
# read from the document. Derived rows carry no arithmetic evidence and cannot back
# a comparison total; above this share the submission must be reviewed by a human.
MATCH_DERIVED_TOTAL_MAX_RATE: float = 0.05

# VAT deviation tolerance: 11.5% (≈13%/113%) + 1% rounding allowance
MATCH_ARITHMETIC_VAT_TOLERANCE: float = 0.125

# Systematic VAT mismatch gate: block when >20% of rows deviate at ~11–12.5%
MATCH_VAT_MISMATCH_BLOCK_RATE: float = 0.20

# Single-row concentration gate: one row must not exceed 60% of total
MATCH_MAX_LINE_CONCENTRATION: float = 0.60

# Declared-total tolerance: 3% before raising a completeness flag
MATCH_DECLARED_TOTAL_TOLERANCE: float = 0.03

# Cosine similarity below this value is flagged as low-confidence match (→ pending)
MATCH_LOW_CONFIDENCE_THRESHOLD: float = 0.70

# Cosine similarity below this value: no credible anchor found (sequential path skipped)
MATCH_SEQUENTIAL_SIM_THRESHOLD: float = 0.50

# Arithmetic validation: fraction of rows that must pass qty×price≈total check (→ REVIEW below)
MATCH_ARITHMETIC_PASS_THRESHOLD: float = 0.90

# Row-level arithmetic tolerance: qty×price vs total_price deviation ratio
MATCH_PRICE_ARITHMETIC_TOLERANCE: float = 0.05


# ── Pre-ingest structural gates (draft_integrity) ───────────────────────────
# 来源：2026-08-09 七份 VL 直出实测。两类缺陷下游都察觉不到——错位后的金额仍是
# "合法的数字"，重复行仍能通过逐行算术校验。故必须在入库前用结构判据拦下。

# 单元格数 ≠ 表头列数的行占比，超过即整份 BLOCKED（实测右移影响 86/90 行）。
# 低于此比例仍逐行 BLOCKED 那些行本身，只是不牵连整份。
INTEGRITY_COLUMN_SHIFT_BLOCKED_RATIO: float = 0.02

# 同上：绝对行数下限，避免小表被比例稀释。
INTEGRITY_COLUMN_SHIFT_BLOCKED_COUNT: int = 3

# 单元格数**少于**表头列数的行占比，超过即整份 BLOCKED。
#
# 与上面两个（格数过多）分开，因为两者性质不同：格数过多必然是解析错误；
# 格数不足则可能合法——注释行、只有两个字段的小计行本来就短。故阈值更宽松，
# 且**只按比例不设绝对行数**：合法短行的数量随文档规模增长（每个分部一行小计），
# 固定行数阈值会在长文档上误报。
#
# 定在 5%：2026-08-10 七份 VL 直出实测，缺格率与真实金额缺陷完全正相关——
# 0% 的四份金额分毫不差；1.1%（泰科龙）错 476 元；5.6%（凯硕）错 2,420 元；
# 13.6%（上海浦东）错 1,246,551 元。5% 拦下后两者，单行短行仍走 REVIEW。
# 此前 missing_cells 无论多少行都只判 REVIEW，导致浦东 279 行里 38 行结构解析
# 失败却以"人工复核"身份放行——那不符合 BLOCKED 的定义（无可靠结构）。
#
# 补记：浦东那次结构崩溃的**根因是方向判错**（只转了 3 页，正确应转 13 页；
# 方向正确时同一份文档六次运行的缺格行全是 0）。这道门是安全网而不是根因修复——
# 方向判定本身不稳定，它兜住的是"方向又错了"的那一次。
INTEGRITY_MISSING_CELL_BLOCKED_RATIO: float = 0.05

# 升级为 BLOCKED 还需要的绝对行数下限。纯比例在小表上会反向失灵：3 行的表里
# 一行短行就是 33%。而**一行短行永远不是"无可靠结构"**，不论表多小——
# 它更可能是一行注释或一行两字段的小计。比例负责"大面积"，这个数负责"不是个例"。
INTEGRITY_MISSING_CELL_BLOCKED_COUNT: int = 3

# 重复行金额占比超过此值 → BLOCKED（实测某份重复使金额虚增 42%）。
# 与 _ARITH_MISMATCH_BLOCKED_AMOUNT_RATIO 同量级：金额层面的错误按 10% 划线。
INTEGRITY_DUPLICATE_BLOCKED_AMOUNT_RATIO: float = 0.10

# 同口径下 数量×单价 与合价的允许偏差。同一税基内这是纯舍入误差，容差应远小于
# 跨税基的 MATCH_ARITHMETIC_VAT_TOLERANCE —— 后者用于区分"税基不一致"而非"算错"。
INTEGRITY_ARITHMETIC_TOLERANCE: float = 0.005

# 明细合计 vs 文件声明总价的**入库阻断**阈值。
# 声明总价是这份文件里唯一不依赖抽取质量的事实，明细之和理应与它吻合到舍入级别
# （136 行两位小数的累积舍入在 2000 万上不到百万分之一）。超出即说明漏行或读错值。
# 定在 0.5%：实测方向判错一页造成的偏差是 0.63%，必须能拦住；而旧的 5% 阈值会放行。
# 声明总价含清单外项目（税费/优惠）时会误拦——那种情况走人工 ack，不放宽阈值。
#
# ⚠ 2026-08-10 审计：上面这条依据只确立了**灵敏度下界**（"至少要能拦住 0.63%"），
# 从未回答"会放过什么"。七份实测：四份有真实缺陷，这个阈值只拦下一份；2000 万的
# 投标可放行 10 万元误差。且比例阈值的**形状**就不对——允许的绝对误差随文档金额
# 放大，而读错一行的代价不随文档大小变。
# 改法与论证见 docs/design/20-checksum-gate-threshold.md（草稿，待批准后实施）。
# **未获批准前不要私自改这个值**——它决定生产拒绝入库什么。
CHECKSUM_BLOCK_DELTA_RATIO: float = 0.005

# 报价口径倍率识别容差：合价/(数量×单价) 落在某个简单倍数附近时按"口径选择"记录，
# 不按算术错误处理。倍率是报价方式的选择，只能观测和标记，禁止据此修正原值。
INTEGRITY_MULTIPLIER_TOLERANCE: float = 0.01

# ── 块级对齐（block_alignment）─────────────────────────────────────────────
# 报价清单的物理顺序不等于招标清单顺序：实测某份投标文件把普通电缆印在前（PDF 2-7 页）、
# 矿物电缆印在后（8-10 页），而采购清单的序号是矿物 1-44、普通 45-136。直接按文档行序
# 对齐会整段错位（实测严格位置命中 0%）。故先做块级对应，再在块内按行序对齐。

# 块指派的数量序列相似度下限。低于此值不算确定性结论，交 LLM 或人工判块级对应。
# 用数量而非金额：招标清单只给序号/名称/规格/单位/数量，价格是各家自己报的，
# 拿价格对块等于用答案对答案。
BLOCK_QTY_SIMILARITY_MIN: float = 0.70

# 最优与次优候选的相似度差距下限。差距过小说明两个块都像，属于歧义，不做确定性判定。
BLOCK_ASSIGN_AMBIGUITY_MARGIN: float = 0.05

# 块内按行序对齐后，允许的逐行冲突占比；超过说明这一块的对应关系本身就不对。
BLOCK_ROW_CONFLICT_MAX_RATE: float = 0.30


# ── 数值截断检测（按列自校准，不假定任何固定宽度或列名）──────────────────
# 判据：某数值列存在硬宽度上限，且卡在上限的值小数位少于该列自身的常见小数位。
# 下面三个是统计有效性护栏，不是格式假设。
INTEGRITY_TRUNCATION_MIN_SAMPLES: int = 20      # 少于此数量的数值样本不下结论
INTEGRITY_TRUNCATION_MIN_SUSPECTS: int = 3      # 疑似截断值少于此数不报（避免个例噪声）
# 第三条判据不需要常数：宽度上限处是否**堆积**，靠上限与次宽两档的实际计数相比即可
# （自然分布向上递减，被截断的列会在上限处堆起来）。见 detect_truncated_numbers。
