# 32 — Comparison without a procurement list

> **Status: A1 + A2 implemented 2026-08-22.** §2 is measurement against the
> real corpus. §5's charter question was decided the same day — see §7 for
> the three decisions and what they map to in code. A3 (the LLM proposer for
> whatever A1+A2 leaves unresolved) is **not built** — deferred per §6
> decision 3, unchanged: on this corpus A1+A2 resolves 100% of rows in both
> projects, so A3 currently has nothing to do.

## 1. Trigger

User, 2026-08-22, after hitting `尚无已确认采购清单（TenderListSession）`:

> 比价核心是货比三家，有采购清单最好，作为对齐的基准，如果没有采购清单也
> 应该可以对齐，先根据顺序对齐，因为报价清单一般是根据采购清单编写的，但是
> 有可能采购清单有多个 […] 顺序对齐之后，还需要大模型再看下是不是对齐了
> […] 有的报价清单上面有章覆盖，可能物料号不一样，但是还是要判断一下。

Two claims to test before designing anything: (a) quotes are written from the
procurement list, so **position carries real information**; (b) comparison is
supplier-vs-supplier, so it should not *require* the tender list at all.

This is not the same problem as design/30. That one generalizes parsing when
a procurement list **exists** but has an unfamiliar shape. This one asks what
happens when there is **no usable list at all**.

## 2. Measured

Structured quote files from both corpus projects, read directly with
`openpyxl` / `csv` — no OCR, no model calls, no cost.

### 2.1 Row counts line up exactly

| Project | File | Raw non-empty rows |
|---|---|---|
| 徐汇 (电缆) | 4 supplier `.csv` | **138 each** |
| 金桥 (阀门) | 3 supplier `.xlsx` | **91 each** |

Every supplier in a project ships the same number of rows. Claim (a) holds.

### 2.2 The quantity sequence is identical across suppliers

Taking the 数量 column (located by header text, so column index does not
matter — it sits at index 8 for 徐汇, and at 5 / 12 / 12 for the three 金桥
suppliers) and comparing position by position:

| Project | Comparison | Positional agreement |
|---|---|---|
| 徐汇 | 亨通 / 宏胜 / 远东 vs 上海浦东 | **100 %** (137/137 each) |
| 金桥 | 凯硕 / 泰科龙 vs 上海绵存 | **100 %** (90/90 each) |

### 2.3 The tender list agrees too — once non-item rows are dropped

Raw positional comparison of 金桥 采购清单 (92 rows) against a quote (90 rows)
gives a misleading **37.8 %**. Dropping rows whose 数量 cell is not a number
(headers, blanks, section separators) leaves 89 items on both sides:

```
压缩掉非数字行后：报价 89 行，采购清单 89 行
逐位相同 89/89 = 100.0%
SequenceMatcher 相似度 100.0%，最长公共块 89
```

So the 37.8 % was **row offset, not disagreement**. This matters twice over:
it validates the whole approach, and it identifies non-item row filtering as
the first thing that has to be right.

### 2.4 The 87 / 89 / 90 discrepancy is a recognition defect, not a document difference

Earlier rounds recorded 凯硕 90 项 / 绵存 87 项 / 泰科龙 89 项. Those numbers
came from the **PDF OCR path**. The suppliers' own `.xlsx` files all carry 89
items with identical quantities. The rows were lost in recognition, not
missing from the documents — which means the alignment problem is much
smaller than it looked whenever structured files are available.

## 3. What the measurements imply

**序号 is the weaker key; the quantity sequence is the stronger one.**
The user proposed aligning by 序号/position. Position works, but the quantity
sequence is strictly better as the *verification* key, for reasons that are
exactly the failure modes the user raised:

| Failure mode | 序号 | Quantity sequence |
|---|---|---|
| Supplier renumbers rows | breaks | unaffected |
| Stamp (盖章) covers name / 物料号 | breaks if the number is covered | quantities are elsewhere on the row |
| Column order differs between suppliers | n/a | unaffected (column found by header) |
| Extra header / section rows | silently shifts everything | detected — the sequence stops matching |

`block_alignment.py` already reached this conclusion from the other
direction, and its module doc says so plainly: 规格文本不可靠, 数量 is the
signal that survives. The same primitive applies here; this is reuse, not a
new mechanism.

**Alignment should therefore be: position proposes, quantity confirms.**
Two rows at the same index whose quantities disagree are not aligned, no
matter what the 序号 says.

## 4. Proposed approach

Ordered so each cut is independently useful. Nothing here is built.

**A1 — non-item row filtering, shared by both sides.** A row is an item iff
its quantity cell parses as a number. This is what turns 92-vs-90 into
89-vs-89. Deterministic, no model, and it is a prerequisite for everything
below. It also fixes a number the user currently sees (采购清单 89 vs 92).

**A2 — anchorless axis.** When no confirmed `TenderListSession` exists,
derive the row axis from the quotes themselves: take the supplier with the
most item rows as the reference, align the others to it by position, and
**verify every pairing against the quantity sequence**. Rows that fail
verification go to the pending lane — they are not silently paired.

The resulting axis is explicitly *not* a `TenderAnchor` set. It carries no
tender-side name/spec/qty authority, only "row i of the reference quote".
See §5 — this is the charter question.

**A3 — LLM re-check, as a proposer only.** Where A2 leaves unpaired or
quantity-mismatched rows (multi-list projects, OCR row loss, stamped cells),
send those rows and their neighbours to the model and ask which rows
correspond. **The model's answer is a candidate, not a decision**: accept it
only when the deterministic layer corroborates (compatible quantity, unit,
and order-preserving position). Anything it proposes without corroboration
becomes a REVIEW item for a human.

This boundary is not negotiable-by-convenience — CLAUDE.md: *the LLM explains
deterministic results […] it may not re-rank candidates, split line items, or
fabricate evaluation facts*. A model that decides alignment is deciding which
supplier's price sits on which row, which is a business fact. Proposing is
allowed; deciding is not.

**A4 — stamped-cell handling.** Out of scope for alignment. If a name or
物料号 is illegible under a stamp, alignment already does not depend on it
(A2 uses position + quantity). The illegible field should be flagged on the
row as unreadable, not guessed. Only if the *quantity* is also covered does
the row become genuinely unalignable — then it is a REVIEW item.

## 5. Charter question — needs an explicit decision

CLAUDE.md §4 states:

> **Identity.** `TenderAnchor` is the only row axis for the procurement list
> and the matrix.

A2 introduces a second kind of row axis. Per CLAUDE.md's own rule
(*never keep two contradicting requirements — fix the documents*), this
cannot be added quietly. The options:

1. **Amend the invariant** to "the matrix has exactly one row axis per
   comparison; it is a `TenderAnchor` set when a confirmed procurement list
   exists, and a quote-derived axis otherwise, and every result states which
   one it used." This keeps the single-axis guarantee (which is what the
   invariant is actually protecting — no mixing) while allowing the axis to
   come from elsewhere.
2. **Keep the invariant** and instead synthesize `TenderAnchor` rows from the
   reference quote, marked with their real provenance. Less doc churn, but it
   makes "anchor" mean two different things, which is the ambiguity the
   invariant exists to prevent. Not recommended.

Whichever is chosen, the downstream consequence is the same and must be
explicit: a comparison on a quote-derived axis has **no tender-side truth to
check against**. It can say "these three suppliers priced the same row
differently"; it cannot say "supplier A missed an item the tender required",
because nothing states what was required. Recommendation and export must
carry that limitation the way `basis="preview"` already does (design/31 cut 1).

## 6. Open decisions

1. §5 — amend the invariant, or synthesize anchors? (Recommend: amend.)
2. Should the anchorless axis be allowed to feed the **official** comparison,
   or only the preview lane? A quote-derived axis is weaker evidence than a
   confirmed tender list; the conservative answer is preview-only until a
   list is confirmed.
3. A3 costs a model call per unresolved block. Acceptable, or should the
   first version ship deterministic-only (A1 + A2) and add A3 after measuring
   how many rows actually survive unresolved? (Recommend: deterministic-only
   first — on this corpus, A1 + A2 resolves 100 % of rows in both projects,
   so A3 would currently have nothing to do.)


## 7. Decisions (2026-08-22) and what they map to

The user made three decisions, resolving §5 and §6:

1. **Amend the invariant** (§5 option 1), not synthesize `TenderAnchor` rows
   under false pretenses. CLAUDE.md §4 Identity now reads: every comparison
   has exactly one row axis, states which kind (`axis_kind`) it is, and a
   quote-derived axis may feed only the preview lane.
2. **Quote-derived axis feeds preview only**, never the official comparison.
   Enforced in the schema contract layer
   (`BidMatrixResult._quote_derived_axis_is_preview_only`), the same way
   design/31 cut 1 enforces `basis`/`recommendation_level` — not by
   `preview_service` remembering to behave, by construction refusing the
   combination.
3. **First cut ships A1 + A2 only.** No LLM re-check step. If deterministic
   alignment (position + quantity, reusing `_sequential_matches` unchanged)
   fails to authorize a submission, its rows fall to the existing embedding
   matcher (same as the tender-anchor path already does) — not to a new
   model call. A3 is a separate future cut, only worth building once real
   usage shows rows that survive both position+quantity and embedding
   unresolved.

### What got built

| Piece | Where |
|---|---|
| A1 — item-row rule (`qty is not None`) | `quote_derived_axis.py::build_quote_derived_axis`, applied when selecting the reference submission and building its anchors |
| A2 — reference selection + synthetic `TenderAnchor` list | `quote_derived_axis.py` — most item rows wins, ties broken by lowest `submission_id` (deterministic, reproducible) |
| Alignment of the *other* submissions to the synthetic axis | **Zero new code.** `import_and_match` → `_sequential_matches` unchanged; its quantity-authorization branch (`qty_ok`) already didn't require DN, so it needed no generalization for this to work |
| `axis_kind` field + preview-only contract guard | `schemas/analysis.py::BidMatrixResult` |
| `preview_service.build_preview_matrix` no longer refuses when there's no confirmed tender list | falls through to `build_quote_derived_axis`; still refuses when there are zero confirmable submissions (an orthogonal, unchanged gate) |
| UI: axis-kind explanation, separate from the `basis=preview` banner | `WorkspaceView.vue` — a second `a-alert` (`type="info"`) that only appears for `axis_kind==='quote_derived'`, stating the limitation in §5's own words |

### Tests

- `test_quote_derived_axis.py` (12) — reference selection, tie-breaking, A1
  filtering, the three-state `document_row_index` ordering (mirrors
  `anchor_match._doc_order`), category filtering, field carry-through.
- `test_preview_service.py` (+3) — end-to-end via real HTTP + `confirm_batch`
  + `import_and_match` + `build_anchor_matrix`: falls back correctly when no
  tender list exists, still refuses when there are zero submissions, and —
  the test that actually proves alignment quality, not just that a matrix
  came back — two suppliers with identical per-position quantities produce
  4/4 `quoted` cells, not cells stuck in `pending`.
- `test_matrix_basis_contract.py` (+4) — the `axis_kind`/`basis` coupling at
  the schema layer.

## 8. 质量门在预览里降级为警告（2026-08-22 手测触发）

手测点「先比价看看（预览）」，凯硕新正一家没过声明总价闭环门，**整个预览
失败**（那次只上传了一家，零 submission 进得去 → `PreviewNotReady`）。

用户的判断：

> 我个人建议先进入预览，其实就是某个供应商的报价没有对齐而已 […]
> 首先，能不能比价是一个等级，有几个能比价是另外一个等级。

### 这次的门是我们自己触发的（实测）

直接读凯硕新正的 `.xlsx`：

| | |
|---|---|
| 有数量的行（真条目） | **89** |
| 无数量的行 | **1** —— 合计行，`合价(含税)` 列写着 932154 |
| 89 条真条目 `合价(含税)` 求和 | **932,154.00** |
| 文件声明总价 | **932,154.00** |
| 系统算出的 `line_sum` / `line_count` | 1,844,723.22 / **90** |

**文件本身分毫不差地自洽。** 是我们把合计行当成了第 90 条报价行，于是总额
被算了两遍，门判 fail。换句话说，用户被挡在门外，去修一个他没有制造、也
修不了的问题。

这正是 §4 A1 那条规则——`qty is None` 就不是条目行——而 A1 当时只落在
派生轴那一侧（`quote_derived_axis.py`），**没有落在入库侧**，尽管 §4 的原文
写的是 "shared by both sides"。这是本轮实现的疏漏，不是设计的缺口。

### 改了什么

`confirm_batch` 把"门失败要不要阻断"从 `dry_run` 里拆出来成独立开关
`gates_advisory`。两者原本焊在一起，但回答的是不同问题：

| 开关 | 回答 |
|---|---|
| `dry_run` | 要不要落库 |
| `gates_advisory` | 门失败了要不要中止 |

预览需要的组合是"照常写（写在沙箱里，外层统一回滚）+ 门只警告"：必须真写，
后面的对齐才读得到；但一家没过门不该让整个预览做不成。`dry_run=True` 隐含
`gates_advisory=True`，既有行为逐字节不变；官方路径两个开关都关，门一点没松
（有回归专门守这一条）。

**降级不是放行。** 门的结论照样算、照样带回调用方（`issues`），
`preview_service` 把它们逐条写进 `notes`（"「凯硕新正」已进入预览，但有
疑点：…"），界面照常显示。悄悄放行比阻断更糟。

## 9. A1 落到入库侧（2026-08-22）

§8 留的那条待办。查真实 job（`12dc7d9af025…`）的原始 items 之后，**两个判据
候选被实测否掉，第三个才成立**——过程记在这里，因为前两个都很像"显然对"。

### 否掉的判据一：无数量即丢弃

同一份文件里 `qty` 为空的行有两条：

    #89  material='缓闭式止回阀'    unit='EPDM'           金额 3,460.00   ← 真条目
    #90  material='含税合价（元）：' unit='含税合价（元）：' 金额 932,154.00 ← 合计行

#89 是列串位造成的识别缺陷（材质 EPDM 落进 unit、数量丢了），行本身是一条
真实报价。按"无数量即丢弃"处理就是**静默删掉 3,460 元**，还顺手让声明总价
闭环门更容易通过——用删行让门通过，正是 CLAUDE.md「等级不得靠静默填充或
下游猜测抬高」要防的东西。

### 否掉的判据二：金额等于其余行之和

看着像个自校验的好信号（合计行本来就等于明细之和），实测不成立：这份文件
前 89 行含税合价求和 **906,614**，合计行写的是 **932,154**，差 25,540——
识别本身丢了 4 行的含税合价。识别质量不完美时它拦不住合计行，而识别完美时
又不需要它。

### 成立的判据（`ingestion/list_rows.py`）

必须同时满足：**该行没有数量**，且有下列任一**正面证据**：

- 名称/规格/单位三列同值且非空——标签串进了所有文本列，正常条目不可能三列
  一模一样。语言无关，不依赖词表。这条抓住了真实那一行。
- 文本命中表尾词表 `FOOTER_MARKERS`。

顺带修了一个一直存在的漏洞：报价侧原有的 `_GRAND_TOTAL_NAME_RE` 里有
`含税合计` 没有 `含税合价`，**一个字之差**，而招标侧
`tender_list._FOOTER_MARKERS` 里本来就有「合价」。两侧各维护一份词表正是
§4 A1 "shared by both sides" 要消灭的东西，现在统一到 `list_rows.py`。

排除的行**不静默**：`confirm_batch` 的返回新增 `aggregate_rows`（含 index /
label / 可直接给用户看的 reason），空列表是常态。

### 效果，以及它没有解决的事

对那份真实数据：90 行 → 排除 1 条合计行 → **89 条入库，那条 qty 丢失的真
条目保留**。声明总价闭环：

| | line_sum | vs 声明 932,154 |
|---|---|---|
| 修复前 | 1,844,723.22 | **97.899 %** |
| A1 之后 | 906,614.00 | **2.74 %** |

**仍然超过 0.5% 的阈值，门照样 fail。** 因为这份文件里还有第二个、独立的
缺陷：识别丢了 4 行的含税合价（#13 蝶阀 DN65、#15 蝶阀 DN100、#19 防污隔断阀
DN25、#59 闸阀 DN65），少了 25,540 元。A1 消掉的是"我们把总额算了两遍"这个
自造问题；剩下的 2.74% 是真实的识别缺陷，**应该继续报警**——那正是这道门存在
的意义。用户能继续往下走，靠的是 §8 的门降级，不是靠 A1 把问题掩盖掉。

下一步（未做）：那 4 行含税合价为什么丢，属于识别层，另开一轮。
