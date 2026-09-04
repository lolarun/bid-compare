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

# ── 报价侧品类推断（2026-08-23）────────────────────────────────────────────
# 品类原本只有招标侧产出（`tender_list_preview` 的多数派 `detected_category`）。
# 招标文件没带采购清单时（实测：某份招标 PDF 的材料明细表整行写"详见附件1"，
# 而附件没有装订进来）品类恒为空，前端 `category` 一直是空串，`batch-confirm`
# 逐份拒收——而界面上**没有任何手动选择品类的控件**，用户到这一步是死路。
# design/32 的"无采购清单也能比价"（报价派生轴）因此在实现上完全不可达。
#
# 修法是让报价行自己投票出品类。实测三份真实语料的把握度：
#   亨通 130/132 电缆 · 远东 135/138 电缆 · 泰科龙 89/89 阀门
# 阈值取 0.8：真实文档的一致度在 0.98 以上，留出的余量足以让"混装多品类的
# 文件"落在阈值之下、退回人工，而不是被多数派硬吞掉。**不达标就留空**，
# 由人工选择——绝不猜一个品类往下走（CLAUDE.md §4：不得靠静默填充抬高等级）。
QUOTE_CATEGORY_VOTE_MIN_SHARE: float = 0.80
# 绝对行数下限：几行的小样本即使 100% 一致也不足以定性，避免被一两行带偏。
QUOTE_CATEGORY_VOTE_MIN_ROWS: int = 5

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

# 识别阶段（ExtractionDraft.compute_quality）的单行算术容差，比入库门更宽。
# 与 INTEGRITY_ARITHMETIC_TOLERANCE(0.005) 有意不同、不是待收敛的分叉（评审
# D5 已核实）：识别阶段的数值可能还没确认，用更严格的容差会把大量待人工核对的
# 正常行提前判 BLOCKED；入库门用严容差是因为那时已经是用户确认过的同口径数值，
# 偏差理应只剩舍入误差。此前是 extraction_draft.py 内的模块级常量，未集中管理，
# 现搬到这里（搬迁不改值）。
EXTRACTION_ARITHMETIC_TOLERANCE: float = 0.03

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


# ── 结构化副本检测（copy_detect）─────────────────────────────────────────────
# 同一份清单在文档内被完整重复打印 K 次（正本/副本）时，按行内容识别第几份。
# 不能要求逐字节相等：每份副本是独立 OCR 出来的，哪怕内容完全一样也会有个别
# 数字/文字读法差异；也不能要求行数精确整除——各份副本可能各自多读/漏读一两行
# （宏胜 137 行、亨通 138 行实测）。改用跟 block_alignment 同一套 SequenceMatcher
# 模糊匹配手法，而不是原先"K 等份、逐行严格相等"的精确匹配（design/26 §P1 复核，
# 后者在真实 OCR 输出上结构性判不出来——精确整除+逐字节相等这两条都是判据
# 形状问题，不是样本不够、调调阈值就能修好的）。

# 判定"这一段是副本"的序列相似度下限。这个值**同时**给 copy_detect.py 两级
# 判据把关，不是只管细粒度那一级：粗粒度（按 name 类目值）判据复用同一个阈值。
# 以后调这个值前两级都要重新核一遍，不能只按整行场景的直觉调。
#
# 0.90 曾经是这个值，后来发现太严——浦东电缆修了"跨行换行名称被拆成两行"
# 的抽取缺陷（paddle_vl._merge_wrapped_rows）之后，两份副本各自的换行/合并
# 次数天然不完全对称，真实副本的类目序列相似度从 0.9747 掉到 0.84（穷举
# 搜索过，不是切点没找对——真实边界处的峰值就是 0.84）。改成 0.80：
# 两段真正不同类目的对照组相似度是 0.0，0.80 到 0.0 之间余量依然巨大，
# 不是为了迁就浦东单个样本把线拉到贴着实测值——同时验证过其余 6 份文档
# （单份清单）在 0.80 下都不会被误判成有副本。
COPY_ROW_SIMILARITY_MIN: float = 0.80

# 至少要这么长的连续区块，才纳入"这是重复副本"的判定——太短的窗口在正常清单里
# 也会偶然出现内容相近，不能算副本边界。
COPY_MIN_BLOCK_LEN: int = 4

# 允许判定的最大副本数。清单当前实测最多两三份（正本+副本×1-2），设更高的上限
# 只是防呆，不是产品约束。
COPY_MAX_COPIES: int = 6

# 两份副本（K=2，最常见的情形）的切点局部搜索范围，占名义切点（n/2）的比例。
# 真实边界不一定精确落在名义中点：两份副本各自独立 OCR，行数、内容修复（比如
# 跨行换行合并）都可能不对称地影响两侧行数，中点±这个范围内搜索最优切点，
# 比只信中点稳（浦东实测：改动过抽取层逻辑后，中点本身的相似度从 0.97 掉到
# 0.82，真实边界偏了但仍在这个搜索范围内）。K>2 时不做局部搜索——内部切点
# 有 K-1 个，各自独立搜索的组合复杂度不值得为"更少见的三份及以上"这个场景
# 增加实现复杂度，维持只试名义切点。
COPY_SPLIT_SEARCH_TOLERANCE_FRAC: float = 0.15


# ── 数值截断检测（按列自校准，不假定任何固定宽度或列名）──────────────────
# 判据：某数值列存在硬宽度上限，且卡在上限的值小数位少于该列自身的常见小数位。
# 下面三个是统计有效性护栏，不是格式假设。
INTEGRITY_TRUNCATION_MIN_SAMPLES: int = 20      # 少于此数量的数值样本不下结论
INTEGRITY_TRUNCATION_MIN_SUSPECTS: int = 3      # 疑似截断值少于此数不报（避免个例噪声）
# 第三条判据不需要常数：宽度上限处是否**堆积**，靠上限与次宽两档的实际计数相比即可
# （自然分布向上递减，被截断的列会在上限处堆起来）。见 detect_truncated_numbers。


# ── 行数守恒的独立证据（docs/design/21 §2.1）──────────────────────────────
# 背景：VL 路径的行数台账是**同义反复**——expected_rows / extracted_rows 都取自
# "模型这一页给了几行"，结构上不可能报出丢行。legacy 那边 expected_rows 来自 OCR
# 的 <tr> 计数，是独立测量。补独立来源之前，任何报告不得声称"未丢行"。
#
# 序号连续性是最便宜的独立判据（免费、且能定位到具体第几行丢了），但**不通用**：
# 2026-08-10 七份实测只有 3 份带序号列（凯硕/泰科龙/远东），另 4 份一行都没有。

# 有序号的行占比低于此值 → 认为这份文档没有可用的序号轴，不做连续性判定
# （而不是拿零星几个序号去推断整份）。
SEQ_COVERAGE_MIN: float = 0.80

# 序号缺口占应有行数的比例，超过即整份 BLOCKED。缺口意味着**确定丢了行**——
# 与"格数不足"那种结构疑点不同，这是行本身不见了，量级到了就是无可靠结构。
SEQ_GAP_BLOCKED_RATIO: float = 0.05


# ── 顺序直连门禁（anchor_match._sequential_direct_connect）──────────────────
# 「顺序直连」= 报价行与招标锚点**按位置一一对应**，不走语义匹配。它是最强的
# 匹配假设，因此门禁必须证明"这个对应不是碰巧"：整表层要求足够多的位置能被
# 独立判据交叉验证，且验证结果高度一致。
#
# 这四个此前是 anchor_match.py 的模块内常数（违反 CLAUDE.md §4「阈值集中」）。
# 搬到这里不改变任何取值与判定行为。

# 判据覆盖率下限：双方都能取到该判据的位置占比。防「稀疏判据蒙混整表通过」——
# 136 行里只有 3 行能比对，即便那 3 行全中也不构成整表按位对齐的证据。
SEQ_EVIDENCE_COVERAGE_MIN: float = 0.90

# 已覆盖位置上的一致率下限。低于此值说明存在整体或局部串位。
SEQ_EVIDENCE_CONSISTENCY_MIN: float = 0.95

# 大类族一致率下限。族判据只作**否决**用（防同规格异品类串位），不单独构成
# 接受依据——名称词表覆盖不到的品类，族判据默认 1.0，不能让它成为唯一通行证。
SEQ_FAMILY_CONSISTENCY_MIN: float = 0.90

# 数量比较容差。此前同一事实在三处有三个值（anchor_match 0.001 / bid_evaluation
# 0.001 / block_alignment 1e-6），严格 1000 倍的那处会把另两处判齐的行判成冲突。
# 三处已统一到这一个常量（评审 D4，2026-08-11）。名字仍带 SEQ_ 前缀是历史遗留
# （最早只服务 anchor_match 的顺序直连判据），语义已是通用数量比较判据。
SEQ_QTY_TOLERANCE: float = 0.001

# ── 保序子序列直连（供应商只报清单的一部分）─────────────────────────────────
# 见 `anchor_match._subsequence_positions`。

# 允许多少比例的锚点"没有数量证据"（数量为空或 0，多为复合行的父行）。
# 0.10：徐汇清单 170 条里有 7 条（4.1%）。放宽到大多数锚点都无数量时，
# "数量序列子序列"这条判据就名存实亡了，必须卡住。
SUBSEQ_MAX_WILDCARD_RATE: float = 0.10

# 断同分时最高分必须领先次高分的差值。0.15 = 实测那一处真实同分
# （预分支电缆头 vs RTXMY-6*50+E25，两者数量同为 2）的分差远大于它，
# 而随手两个相似型号串之间的分差通常在 0.05 以内。断不开就整份回落语义，
# 不留"大概是这个"。
SUBSEQ_TIEBREAK_MARGIN: float = 0.15

# ── 分类筛页（docs/design/41）────────────────────────────────────────────────
# 只把"真的是报价清单"的页送去 Paddle（¥0.09/页）。分类侧走小米 MiMo 订阅制，
# 实测泰科龙 53 页分类耗 46,232 token ≈ ¥0.0004，相对 Paddle 可忽略。
# 阈值/模型集中在这里，不散落在 intelligence 里（CLAUDE.md §4「阈值集中」）。
# **显式开关，跟凭据分开**（2026-08-28）。此前"默认关闭"的实现方式就是
# `MIMO_API_KEY` 没配——可是 2026-08-27 起 TEXT/VISION_CLIENT_VENDOR 默认也是
# mimo，两者读同一个环境变量：为了让厂商默认真正生效而配上 key 的那一刻，会连
# 带把筛页一起打开，而筛页的取舍（省 79% Paddle 费用、端到端慢 33%）在
# docs/spec/TECHNICAL.md §8 里是**尚未做出的产品决策**。一个环境变量不能替用户
# 决定两件不相干的事，所以开关独立出来，默认 False；key 仍是必要条件（没 key
# 分类器就是 None，整份送，逐字节等于接入前）。
PAGE_FILTER_ENABLED: bool = False
PAGE_FILTER_MODEL: str = "mimo-v2.5"
PAGE_FILTER_BASE_URL: str = "https://token-plan-cn.xiaomimimo.com/v1"
# 一个 8 页窗口要输出 8 行判定；实测 reasoning_tokens≈0，4000 足够有余。
PAGE_FILTER_MAX_TOKENS: int = 4000
# mimo 视觉调用（补位 gap_fill、招标 VL-direct 回落）的单次请求超时。之前没配，
# 2026-08-27 项目159 三份文件卡在"补读缺失金额"7-9 分钟无任何日志后被孤儿任务
# 清扫杀掉——openai SDK 不设超时时默认等 600s，一页三个方向顺序试，足够拖到
# 清扫线。跟 paddle_doc_meta.py 的文本调用同一个 60s 惯例对齐；给视觉留 30s
# 余量（多张图 + 更大 prompt），不是另起一套阈值。
MIMO_VISION_TIMEOUT_S: int = 90

# ── OpenAI 兼容客户端的重试与超时（2026-09-02）──────────────────────────────
# openai SDK 自己的默认值是 `max_retries=2`、**超时 600s**——两个都是厂商默认，
# 不是本项目做过的决定，而 CLAUDE.md §4「阈值集中」要求的正是把这类数字命名
# 下来。补这两个常量的直接起因：mimo 自 2026-08-27 起是 TEXT/VISION 的默认
# 厂商，它这一侧从未显式声明过重试策略；dashscope 那侧却有一整套手写的 5 次
# 退避（`dashscope_ocr._MAX_RETRIES`）——**重试最弱的恰恰是默认路径**，而这个
# 事实在代码里看不出来，得去翻 SDK 源码才知道。
#
# 两层重试不叠加：`dashscope_ocr` 那圈手写循环所用的 client 显式传
# `max_retries=0`。不这么做就是 5 × (1+2) = 15 次尝试，最坏情况足以把单次调用
# 拖过孤儿任务清扫线（`main.STUCK_JOB_MAX_AGE_MINUTES` = 30 分钟）。
#
# **3 是显式选择，不是实测得出的最优值**：SDK 默认的 2 在一次 429 退避窗口里
# 偏紧，而 dashscope 的 5 是为 OCR 批量高并发（每 key 6 并发、429 频发）定的，
# 单次文本/视觉调用不需要那么多。有实测数据后应据此调整，别把它当成已验证阈值。
LLM_MAX_RETRIES: int = 3
# 文本类调用的默认超时，沿用 `paddle_doc_meta` 已有的 60s 惯例；视觉类调用另用
# 上面的 MIMO_VISION_TIMEOUT_S（90s，多图 + 更大 prompt）。最坏耗时 = 超时 ×
# (1 + LLM_MAX_RETRIES)：文本 60×4 = 240s、视觉 90×4 = 360s，都仍在清扫线之内。
LLM_TIMEOUT_S: int = 60

# 文本类调用（封面标量、招标要求、卡片概述）走哪家。design/41 的调查里 mimo
# 在同等条件下准确率不输 qwen 且订阅制成本近乎为零，这三项失败后果各不相同
# （概述错了只是文案难看，封面标量错了会影响声明总价核对门），所以是一个开关
# 逐项切、而不是一次性硬换。**2026-08-27 由 dashscope 改为 mimo 默认**——用户
# 明确要求"全部切换为 mimo"（9 处依赖里 8 处已实测验证过，见
# docs/spec/TECHNICAL.md §4「Vendor / provider switches」），此前只做了"能切"没有真的切，是漏了
# 一步而非用户认可的决定。没有 MIMO_API_KEY 时仍然回落 dashscope 并记日志
# （`test_text_client_switch.py` 断言这条不能变成静默降级）。
TEXT_CLIENT_VENDOR: str = "mimo"        # 'dashscope' | 'mimo'
# 视觉类调用（扫描件招/投标判定、空格子补位、招标 VL-direct 回落）走哪家。
# 跟文本分开一个开关：两类调用的失败后果、验证方式都不同，捆在一起切换等于
# 逼人一次性接受两种风险。**embedding 不在这两个开关的管辖范围**——mimo 没有
# embedding 接口，对齐兜底（`anchor_match._embed`）只能是 dashscope，那是硬约束，
# 这一条切不了、也不该被这次改动误当成遗漏。同样 2026-08-27 改为 mimo 默认。
VISION_CLIENT_VENDOR: str = "mimo"      # 'dashscope' | 'mimo'

# ── 列→角色映射的确定性验证（design/40 §5.1）────────────────────────────────
# 见 `intelligence/column_roles.verify_roles`。这三个阈值是**模型提议能否被采纳**
# 的唯一闸门，也同样用来判词表结果够不够格（验证器对提议来源中立）。

# 数值型列（数量/单价/合价/税率/税额）的非空取值里，能解析成数的占比下限。
# 0.95 而不是 1.0：真实表里混一两个"面议"/"/"是常态，不该据此判定整列认错。
COLUMN_ROLE_NUMERIC_MIN_RATE: float = 0.95

# 名称列的非空取值里，"不是纯数字"的占比下限。整列都是数字 = 认成了序号或数量。
# 0.80 留出余量：型号本身是纯数字的条目（"4"、"110"）真实存在。
COLUMN_ROLE_TEXT_MIN_RATE: float = 0.80

# `数量 × 单价 ≈ 合价` 的闭合率下限，**同税基配对**下评估。
# 0.85 而不是更高：泰科龙那种原文就有空洞、绵存那种按根/按套报价的倍率行都会
# 拉低闭合率，它们是已知的真实形态（design/33、`_PLAUSIBLE_MULTIPLIERS`），
# 不该让列映射为此背锅。低于 0.85 才是"这三列里有一列认错了"的量级。
COLUMN_ROLE_ARITHMETIC_MIN_RATE: float = 0.85


# ── 识别进度估算（design/27 §6）─────────────────────────────────────────────
# Paddle 云端识别是"提交+轮询"的单一长阶段，百度轮询响应只有一个状态字段
# （running/success/failed），没有逐页/逐步细分（design/26 §9 已核实并订正过
# 早期草案里"逐页进度天然可得"的错误假设）。唯一能诚实展示的进度信号是
# "已耗时 ÷ 预计耗时"，预计耗时按页数线性估算，估算封顶展示 95%（不能让进度
# 条在识别真正完成前显示 100%，那是编造）。
#
# 每页预计耗时——由 design/26 §6 P2b 真实生产路径调用的两个参照点反推：
# 11 页文档≈20s，53 页文档≈85s（design/27 §6 引用的原始表述）。
# 两点分别得 20/11≈1.82s/页、85/53≈1.60s/页，取平均 1.7s/页作单一常量
# （比线性回归的斜率+截距形式简单，且两点本身已经是"典型值"不是精确测量，
# 没必要为一个粗估算配一条两参数公式）。
#
# **这只是两点拟合，不是实测分布**（复核意见，2026-08-13）：仅由 P2b 21 次
# 调用里的 2 个文档反推，样本量不足以称为"分布"。它作为进度条估算常量够用
# ——估错的代价只是进度条不够准，不是判据（不影响任何阻断/放行决策）——但
# 不能被当成"已验证的每页耗时"引用到别处。design/26 P2 系列跑更多文档、
# 有更大样本后应当重新标定，标定时保留这条注释的推导方式，不要只改数字
# 不留痕迹。
PADDLE_EXPECTED_SECONDS_PER_PAGE: float = 1.7

# 识别中的估算进度封顶——即使 已耗时/预计耗时 算出来 ≥100%，界面也只能显示
# "预计快完成了"而不是"已完成"，真正的完成态由轮询拿到 status=success 触发，
# 不能被这条估算线抢先报告。
PADDLE_PROGRESS_ESTIMATE_CAP: float = 0.95
