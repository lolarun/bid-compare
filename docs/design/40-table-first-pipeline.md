# 40 — Table-first pipeline: where the model calls go, and why the misalignment keeps coming back

> ## RETRACTION 2026-08-23 — §2 and §7 were wrong, measured
>
> **§2 claimed the pipeline flattens the grid to CSV and then re-derives column
> identity from the text. That is not what it does.** `build_quote_csv` computes
> `_classify_columns(header)` per table and writes **canonical slot names** to the
> CSV, never the original Chinese headers (a comment at that site records that
> writing raw headers was tried and reverted for exactly the collision §2
> imagines). The round-trip is `slots → CSV → slots`, which is not where accuracy
> is lost.
>
> **The per-row tax-rate anchor in `_extract_row_fields` is a correct
> compensation, not the defect §2 called it.** Probe over all 7 snapshots: the
> anchor and the header mapping disagree on 15 rows, and on every sampled one the
> **anchor is right** — Paddle compresses a row when 材质 sub-columns are empty,
> so header positions shift left. Header-indexed extraction returns `'个'` for
> 数量 on those rows. Reverting to it would be a regression.
>
> **§7 claimed L0 was "the single change with the largest expected effect" on
> accuracy. Measured: it has no effect on accuracy at all.** Every unit of amount
> loss in the corpus is a row where Paddle returned *no amount cells whatsoever*:
>
> | doc | gap | explained by rows with no amounts |
> |---|---|---|
> | 泰科龙 | −26.22% | **100%** — 9 rows, all on page 10, ¥247,682.78 |
> | 亨通 | −7.82% | ~96% |
> | 浦东 | −7.20% | ~67% |
>
> For 泰科龙 those rows carry name and spec but nothing else, and 5 of the 9 unit
> prices (`774.47`, `70.09`, `200.53`, `240.44`, `306.64`) occur **zero times in
> the entire Paddle response**. The values are not in the input. No restructuring
> of extraction code can recover them.
>
> **What actually fixes these numbers is design/33 (gap fill)** — approved by the
> user 2026-08-22, still unimplemented. §5's layering and §6's shipped L1 stand;
> only the L0 rationale is withdrawn.


> **Status: §2–§4 are measured facts about the code as it stands 2026-08-23.
> §5 is the proposed sequence. §6 is what is being built now; §7 is what is
> recorded but not scheduled.**

## 1. The two questions

> 我不知道现在识别完是CSV文本么，怎么总有串行和对齐的问题呢

> 再规划一遍 识别、解析、对齐 中涉及到模型的顺序及算法，尽量让调用达到最佳性价比

They turn out to be one question. The recurring misalignment and the awkward
place the model calls sit both come from the same shape: **a grid is flattened
into text and then re-parsed back into a grid.**

## 2. Yes — recognition output is CSV text, and that is the problem

```
Paddle 结构化 JSON     pages[].tables[].{ cells[], matrix[r][c] }
                        ← matrix[r][c] is an index into cells[]:
                          the column position of every value is KNOWN
   ↓  build_quote_csv()     serialize the grid to CSV text
CSV 文本
   ↓  parse_csv()           re-derive每列的身份 from the text
ExtractionDraft
```

The column coordinates exist, and we throw them away — then spend real
complexity guessing them back.

The clearest evidence is `paddle_vl._classify_trailing_cells`, whose own
docstring states the design:

> 税额列之后的剩余列（含税单价/含税合计/品牌/备注，**具体几列因行而异**）
> **逐行**按算术关系正着认——**不管原表头在这几个位置写的是什么字**。

Tail columns are identified **per row**, from arithmetic, with the header
explicitly ignored. A per-row identity decision is a per-row opportunity to be
wrong, and the same docstring records one it cannot resolve: when `数量 = 1`,
含税单价 and 含税合计 hold the same number and two slots compete for one
candidate.

`build_quote_csv` carries a second scar in the same vein — a comment recording
that re-feeding original Chinese header text through the CSV round-trip once
shifted `spec` into 型号 and dragged `qty`/`price` along with it.

This is also the honest root of the defect classes already documented
separately: design/34's within-row column shift and design/37's cross-row
misalignment are both downstream of "column identity is inferred, not read".

## 3. What the model calls cost today

Per document, current code:

| 阶段 | 输入 | 模型调用 |
|---|---|---|
| Tier 0 分类 | xlsx / csv | **0** |
| | PDF 原生文字层 | **0**（封面关键词判据） |
| | PDF 扫描件 | **1 次视觉**（实测 6.5–9s） |
| 识别 | 报价 PDF | **1 次 Paddle 提交**（整份一次）+ 1 次纯文本（封面标量/品牌要求） |
| | 招标 PDF 有文字层 | **0**（`tender_text_layer` 直抽）—— 但招标要求抽取仍走 qwen 视觉，是全仓库最后一处 qwen 依赖 |
| | 报价 xlsx / csv | **0** |
| 解析 | 全部 | **0** |
| 对齐 | 顺序 / 子序列直连 | **0** |
| | 语义回落 | **嵌入**：锚点 + 全部报价行，每批 10 条。徐汇 170 锚点 + 4×136 报价 ≈ 714 条 ≈ **72 次调用** |
| 解释 | 矩阵洞察 | 1 次，按需 |

Two things stand out.

**The expensive one is alignment fallback, and it is now mostly avoidable.**
72 embedding calls to align a set that design/39 solves deterministically at
100%. Every case that direct-connect accepts is 72 calls saved. The cheapest
optimization available is therefore *not* a cheaper model — it is making the
deterministic path apply more often.

**The per-row tail inference costs zero calls and buys negative value.** It is
not a saving; it is complexity spent to reconstruct information we deleted one
function earlier.

## 4. Why a CSV blob is also the wrong thing to hand a model

If we do ask a model about a table, CSV text is close to the worst input:

- the model has to re-infer the grid before it can answer anything about it;
- ragged rows and embedded commas/newlines make that inference unreliable;
- there is no stable handle to *refer to a column by* — so the answer comes back
  as prose or as header strings, which then have to be matched back.

The friendly form is the one the grid already has: **a header list plus a few
rows, addressed by integer column index.** The model answers
`{"quantity": 4, "unit_price_excl_tax": 5}` — indices, not names — and the
answer is directly usable and directly checkable.

So the LLM-friendly representation and the misalignment-proof representation are
**the same representation**. That is the whole design.

## 5. The proposed sequence

One rule underneath it: **models answer questions about *schema*, never about
*rows*.** Schema answers are verifiable by arithmetic before anything is stored;
row answers are not falsifiable by any independent evidence, which is why
CLAUDE.md forbids them.

| 层 | 做什么 | 模型调用 |
|---|---|---|
| **L0 网格** | 任何来源（Excel / CSV / Paddle `matrix`）统一成 `header: list[str]` + `rows: list[list[str]]`。**不再经过 CSV 文本往返。** | 0 |
| **L1 列→角色** | 每张表**一次**。词表 → 验证 → 验不过才问模型 → 再验一次。 | 已知形状 **0**；新形状 **1** |
| **L2 取值** | 纯按下标取。**不做任何逐行推断。** | 0 |
| **L3 行校验** | 算术恒等式 → `validation_flags`。只标记不修正。 | 0 |
| **L4 对齐** | 数量序列保序子序列（design/39）→ 拒绝时才回落嵌入。 | 直连 **0**；回落 ~72 |
| **L5 解释** | 只读确定性结果。 | 1，按需 |

Good-path totals per document: **1** model call for a PDF (the Paddle submit),
**0** for a spreadsheet. Schema: **0** on known shapes. Alignment: **0**.

L1 is where the generalization the user asked for actually lives, and it is
cheap precisely because it is per-table rather than per-row: one call amortized
over 89–170 rows, and only for shapes the keyword table has never seen.

### 5.1 Why verification has to exist before the model is allowed in

`column_roles.verify_roles` asks only what the data can answer:

- 数量/价格/税率列的非空取值，绝大多数要能解析成数；
- 名称列不能整列是数字；
- **同税基**配对下 `数量 × 单价 ≈ 合价` 的闭合率要够高。

It is neutral about who proposed the mapping — which is exactly what qualifies
it as the gate. A model that mis-assigns 数量 and 单价 collapses the arithmetic
rate and is rejected before a single row is stored. That is a fundamentally
different risk profile from letting a model decide *which quote row is which
anchor*, where no independent evidence exists to contradict it.

## 6. Built this round (L1)

`apps/api/intelligence/column_roles.py` — one role vocabulary (`ROLE_LABELS`)
shared by the keyword table, the prompt and the verifier; `verify_roles`; and two
proposers (`propose_by_llm` for columns, `propose_layout_by_llm` for
header-row + columns).

Wired as **keyword → verify → model only on failure → verify** into:

- `tabular_ingestion.resolve_columns` (quote side), recording
  `_doc_meta.column_source` = `keyword` / `llm` / `keyword_unverified` plus the
  failing reasons. `keyword_unverified` is kept deliberately: failing
  verification is not the same as failing to parse — a missing optional column
  (品牌, 备注) should not reject an upload, and the fatal case (no name column)
  is still refused separately.
- `tender_list._layout_by_llm` (anchor side), which needs the *layout* proposer
  because `_find_header_row` cannot even locate the header row when the header
  words are unknown — column roles alone would be useless there.

Measured on the nine-file zero-model corpus: all nine resolve via `keyword`,
**zero model calls**, item counts unchanged (89/89/89/90 and 136×4). A test with
a deliberately exploding client factory pins that.

Generalization is demonstrated end to end rather than asserted: a real quote CSV
with `材料/设备名称 → 物资描述` and `数量 → 用量` fails the keyword path (proved by
its own test), and with a stubbed proposer parses to **item-for-item identical
output**. The stub keeps it offline and deterministic — the thing under test is
the wiring and the gate, not a model's form on the day.

### 6.1 One thing the gate cannot do, stated plainly

**Multiplication is commutative, so `verify_roles` cannot catch 数量 ↔ 单价
being swapped.** `数量×单价` and `单价×数量` are identical row for row, and both
columns are numeric so the type check is silent too. The first version of that
test asserted arithmetic *would* catch it; running it disproved that, and the
test now pins the true behaviour with a note to update it if a real discriminator
ever appears.

No single-file evidence falsifies it: quantities here are not integers (1905.25,
2882.94) and amounts are not reliably two-decimal. The evidence that *would*
falsify it is cross-supplier — same item, same quantity across bidders, different
unit price — and that only exists at alignment time.

So the risk is narrowed structurally instead of pretended away: when the keyword
table failed **only because roles were missing**, the model may fill empty slots
but may not overwrite a role the keyword table identified from the header text
(`_only_missing` / `_merge_proposal`). A sheet whose 数量 column is literally
named 数量 is therefore untouchable by the model; a sheet where it is not named
recognizably had no second opinion to contradict anyway. What remains is recorded
as `column_source="llm"` and is auditable.

## 7. Recorded, not scheduled

**L0 — remove the CSV round-trip in the Paddle path.** `build_quote_csv` →
`parse_csv` should become `matrix` → grid → L1/L2. This deletes the reason
`_classify_trailing_cells` exists at all: with column identity read once from the
header by index, the tail columns need no per-row arithmetic guessing, and the
`数量 = 1` ambiguity it documents disappears rather than being worked around.

It is not in this round because it moves the whole recognition path and every
snapshot baseline with it; it deserves its own measured before/after against the
seven committed snapshots. It is, however, the single change with the largest
expected effect on the defect classes in design/34 and design/37 — those are
symptoms, and this is the cause.

**Cheap follow-ups worth measuring when L0 lands:**

- The scanned-PDF Tier 1.5 vision call (6.5–9s) classifies a document we are
  about to recognize anyway. For files already destined for recognition, the
  recognition output answers the same question for free.
- `tender_text_layer` still calls qwen vision for 招标要求 — the last qwen
  dependency in the tree. `paddle_doc_meta`'s plain-text path already covers the
  equivalent need on the quote side.
