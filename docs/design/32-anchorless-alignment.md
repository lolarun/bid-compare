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
