# 33 — Filling recognition gaps with a second model

> **Status: proposal accepted in principle 2026-08-22, nothing built — and its
> central measurement was corrected the same day, see §2.6. The corrected scope
> is 11 rows of 938 (1.2%), not the 80 (8.5%) this document first claimed.
> Re-read §3 before committing to it.** §2 is measurement against the real
> corpus (7 quote documents, 13 live model calls).
> The two charter questions in §5 were both decided the same day — see §5.1
> (rule carve-out, accepted) and §5.2 (the Paddle-only alternatives were tested
> and rejected on evidence).
>
> **Implemented 2026-08-23** — `apps/api/intelligence/gap_fill.py`. §6 records
> how the four open decisions were settled and re-measures the payoff, which is
> larger than §2.6 implied: those 11 rows are 100% of 泰科龙's −26.22% gap.
>
> **Correction, same day, found on re-verification.** §6 claimed all four
> decisions were "pinned by a test." Decision ② (filled amounts stay out of the
> declared-total checksum) was not — `line_total_sum` in
> `quote_confirmation_service.py` summed every row unconditionally, and
> `test_gap_fill.py` never mentioned checksums. Fixed and covered by
> `test_checksum_gate.py::test_gap_filled_amounts_are_excluded_from_the_declared_total_checksum`,
> verified red→green (reverting the fix fails the test). §4.3's UI requirement
> ("filled cells must render differently from read cells") is **not built** —
> `field_sources` is computed but discarded before it reaches `job.result`, and
> the live grid component (`QuoteGrid`, Univer-based) does not render
> `validation_flags` at all, not even at row level. See HANDOFF.md's
> 2026-08-23 entry for the full account of what was verified true vs. false.

## 1. Trigger

User, 2026-08-22, after seeing that 泰科龙 still shows 9 rows with no price at
all even after the `merge_tables` fix:

> 假设 Paddle 识别完，有空位，是否可以通过之前使用的多模态大模型进行尽快补位

Two things have to be true before this is worth building: the gaps must be
**engine loss rather than genuine no-quote**, and the second model must read
those exact cells **correctly**. Both are measurable without writing any
production code, so they were measured first.

## 2. Measured

### 2.1 The gaps are not rare, and not one document's quirk

Every quote fixture, recognised through the production path
(`recognize_quote_paddle`, `merge_tables=False` as of 2026-08-22):

| Document | Detail rows | Rows with **no** total price | Other per-field gaps |
|---|---|---|---|
| 泰科龙 | 89 | **9** | qty 19, 税率 9, 税额 9 |
| 凯硕 | 90 | 0 | **含税合价 4**, 含税单价 8, qty 2 |
| 绵存 | 89 | 1 | — |
| 亨通 | 132 | **11** | 单价 6, qty 1 |
| 宏胜 | 136 | 1 | 单价 1, qty 1 |
| 浦东 | 264 | **20** | qty 1 |
| 远东 | 138 | **38** | qty 17, 单价 6 |
| **total** | **938** | **80 (8.5%)** | — |

> **This table's middle column does not mean what §1 needs it to mean.** "No
> total price" turns out to be two unrelated defects added together — only 11
> of the 80 are empty cells. See §2.6, written after the table above.

The 凯硕 row is the same one design/32 closed with «那 4 行含税合价为什么丢，
属于识别层，另开一轮». Whatever the mix turns out to be, it is not a 泰科龙
oddity — it reaches every document except 凯硕.

### 2.2 The gaps are engine loss, not "the supplier did not quote"

泰科龙 seq 60–68 (page 10) come back with columns 8–18 as empty strings while
seq 59 on the same page is complete. Checked both ways:

- the rendered scan prints every value (`60 → 242 / 629.96 / 152449.29 / 13% /
  19818.41 / 172267.70`);
- the supplier's own Excel carries the identical numbers.

So the printed document has the data and Paddle returned blanks. This holds
across all three `merge_tables` arms (old snapshot, P2 run1, today's run) —
the same 9 seq every time, so it is deterministic engine behaviour, not drift.

This matters for vocabulary. `draft_integrity.classify_amount_cell` already
separates `AMOUNT_NOT_QUOTED` (the cell literally prints `/`, `无`) from
`AMOUNT_EMPTY` (nothing was read). These 9 rows are `AMOUNT_EMPTY`. The
charter's «"原文明确不报价"与"读不到"必须分开标记» is currently honoured, and
this design must not blur it: a filled cell is a **third** provenance, not a
promotion of either.

### 2.3 The second model recovers the missing cells — at one orientation, with omissions

Page 10 rendered to PNG and sent to `qwen3.7-plus` via the existing
`DashScopeOCRProvider.vl_extract_csv`, `temperature=0`, asked for the numeric
columns only.

Orientation first, one call each:

| Input | Result |
|---|---|
| as rendered (content lying sideways) | refused — `DataInspectionFailed` 400, most likely the red seal |
| rotated **90°** | all nine rows correct |
| rotated 270° | 税额 column blank, 价税合计 filled with the **税额** value |

Then the 90° arm three times, scored on all five money/quantity fields of
seq 60–68 against the supplier Excel:

| Run | Rows fully correct | Deviation |
|---|---|---|
| 1 | **9 / 9** | — |
| 2 | 7 / 9 | seq 65, 66 returned **qty empty**; every money field correct |
| 3 | 7 / 9 | identical to run 2 |

Four findings, in order of importance:

1. **Across all three runs the model never returned a wrong value — only
   missing ones.** The failure mode at the correct orientation is omission,
   which is the failure mode we can live with: a blank stays a blank.
2. **A wrong orientation does not fail loudly — it returns a well-formed,
   plausible, wrong number.** The 270° arm put `19818.41` into seq 60's
   价税合计; that is the row's tax amount. Nothing in the response says so.
3. It is not deterministic. 9/9 once, 7/9 twice. Any claim of a fixed recovery
   rate would be false; the design must treat per-field recovery as best-effort.
4. Even the good arm mis-reads structure: an earlier variant of the prompt that
   also asked for `unit` got `给排水` back (that is the 专业 column). Correct
   numbers do not imply a correct row.

Finding 2 is the whole risk of this feature. Prices are money; a silently
shifted column is worse than a blank.

### 2.4 The arithmetic identity separates the two orientations cleanly

Run the 90° and 270° answers through the identities the codebase already uses,
at the production tolerance (`EXTRACTION_ARITHMETIC_TOLERANCE = 0.03`):

`qty × unit_price ≈ total` · `total × tax_rate ≈ tax` · `total × (1+rate) ≈ incl`

| Arm | Rows passing |
|---|---|
| rotated 90° (correct) | **9 / 9** |
| rotated 270° (shifted) | **0 / 9** |

Perfect separation, no tuning, no new threshold. This is the load-bearing
result: a fill can be **verified structurally** rather than trusted.

Note the interaction with finding 2.3.3: when qty comes back empty (seq 65/66
on runs 2–3) the first identity is not evaluable, but the two tax identities
still are, so the money fields remain verifiable and qty simply stays blank.
Partial verifiability is the normal case, not an edge case — §4.4.

### 2.5 Paddle cannot fill its own gaps — both variants tested and rejected

The alternative that would avoid a second engine entirely, tested because §5.2
depended on it:

| Variant | Runs | Result |
|---|---|---|
| page 10 submitted **alone**, original orientation | 3 | **Identical blanks**, byte-for-byte. Not a context or merge effect. |
| page 10 submitted **alone, rotated upright** | 2 | Raw cells **do** contain the numbers (seq 60–63 match the Excel), but the response shape is unusable: every row duplicated with a material-continuation row, and rows whose 型号 is blank shift a column. End-to-end through `build_quote_csv` with the document's real header supplied: **0 / 10 rows correct** — values land on the wrong seq. |

The upright-Paddle variant is not simply "worse"; it is *dangerous* in the same
way as the 270° qwen arm — it produces values attributed to the wrong row.
Recovering it would need a bespoke parser for a second response shape, which is
more code and more risk than the qwen path, whose output is a clean CSV.

This matches the earlier 2026-08-22 finding that pre-rotating a two-page
extract collapsed Paddle's recall (HANDOFF.md, arms A/B). Upright input is not
a free improvement for this engine.

### 2.6 Correction: 69 of the 80 are column shift, not empty cells

User, 2026-08-22, on reading §2.1: «为什么绝大多数都是OK的，就这么几个页面读出来
是空». Checking that instead of assuming turned up the answer: most of the 80 are
not blank at all.

远东 page 3 is the clearest case. Rows 1–2 come back with all 9 cells correct
(`数量 1905.25 / 单价 873.57 / 合价 1664369.25`). From row 3 on, **Paddle drops
the 数量 cell entirely**, so every later column slides left by one — 合价 lands
in 单价, 备注 lands in 合价 — and the 合价 slot ends up empty. The values are
present and wrong, not absent. The same response also repeats `150009.49` across
three consecutive rows: this is the **same vertical run-length smearing** that
produces 泰科龙's 名称 column errors (HANDOFF.md), not a separate phenomenon.

Classifying every one of the 80 rows by its raw Paddle row — few cells left and
at most one number ⇒ genuine hole; near-full width with numbers still present
⇒ shift:

| Document | No total price | Genuine empty | Column shift |
|---|---|---|---|
| 泰科龙 | 9 | **9** | 0 |
| 远东 | 38 | 2 | **36** |
| 浦东 | 20 | 0 | **20** |
| 亨通 | 11 | 0 | **11** |
| 绵存 | 1 | 0 | 1 |
| 宏胜 | 1 | 0 | 1 |
| 凯硕 | 0 | 0 | 0 |
| **total** | **80** | **11 (1.2%)** | **69 (7.4%)** |

(The split is a heuristic on cell occupancy; boundary rows exist. The order of
magnitude is the point, not the exact 11/69.)

The genuine holes are concentrated exactly where the user's intuition said they
would be: 9 of the 11 are 泰科龙 page 10, and they share one signature — the
last non-empty cell is `+EPDM`, the tail of a **wrapped 材质 cell**. A cell that
breaks across two printed lines eats the rest of the row.

**What this does to the proposal:**

- The addressable scope is **11 rows (1.2%)**, not 80. §1's premise stands but
  is an order of magnitude smaller than §2.1 suggested.
- For the 69 shifted rows a gap-fill is the **wrong treatment**, not merely an
  ineffective one. Their 合价 is empty as a *consequence* of the shift; filling
  it while leaving the wrong 数量 and 单价 in place manufactures a row that
  looks complete and is not. The arithmetic gate (§2.4) would reject most such
  fills — which is the gate working, but it means the feature would spend three
  model calls per page to achieve nothing on 7.4% of rows.
- The 69 rows need the column-shift defect fixed instead. `_locate_tax_rate_idx`
  already exists for exactly this failure mode on the 报价 side, but the 徐汇
  documents have **no tax column**, so that anchor cannot fire — they need a
  different per-row anchor. That is a separate design, not this one.

## 3. What the measurements imply

- The feature addresses **11 rows of 938 (1.2%)** — see §2.6. Whether that
  justifies a second engine, a permanent qwen dependency, and three model calls
  per affected page is now a real question, and it is the first thing to settle
  before writing code. The measured recovery on those rows is good; the
  population is small.
- The larger defect (69 rows, 7.4%) is column shift and is **out of scope for
  this design** — filling those cells would paper over a misalignment.
- It must never be trusted on the model's say-so. The gate in §2.4 is not a
  nice-to-have; without it, finding 2.3.2 turns this feature into a machine for
  inventing prices.
- Orientation cannot be assumed, and it also does not need to be *detected*
  separately — see §4.2.
- Recovery is partial and varies run to run. The design must not promise a
  rate, and must leave un-recovered cells blank without complaint.
- Scope is **empty cells only**. 泰科龙's other nine bad rows (44–49, 82–84)
  have values that are wrong, not missing. Filling those means overwriting a
  recognised value, which CLAUDE.md §4 forbids without confirmation. They stay
  out of this design.

## 4. Proposed approach

### 4.1 Trigger

After `build_draft`, collect rows where an amount slot the table **has as a
column** classifies `AMOUNT_EMPTY`. Group by `source_ref.page`. Nothing else
triggers a re-read — not low confidence, not a failed total, not a hunch.
"The engine returned nothing for a column that exists" is the only condition,
and it is decidable from the draft alone.

泰科龙: 1 page of 53. 远东: the worst case in the corpus, still a minority of
its 19.

### 4.2 Orientation: selected by the gate, not detected separately

Do not add an orientation probe. Send the page at 0° / 90° / 270°, and keep
the arm whose returned rows **pass §2.4**. The gate has to run anyway, so this
costs nothing beyond the extra calls, and it fails safe: if no arm passes,
nothing is filled. The 0° arm must tolerate `DataInspectionFailed` as a normal
outcome (§2.3), not an error worth surfacing.

Cheaper variant to decide in §6: try one orientation first (inheriting the
document's dominant angle if known) and only fan out when it fails the gate.

### 4.3 Writing the fill back

- **Only into cells that are empty.** Never touch a cell with a value.
- `field_sources[field] = "llm"` — the vocabulary already exists in
  `DraftRow.field_sources` (`direct_cell|missing|derived|llm`), unused so far.
- Add a `validation_flag` naming the fill so it is visible in the doubt inbox.
- **The quality tier does not move.** CLAUDE.md §4: «Tiers must never be
  raised by silent fill or downstream guessing». A row that was REVIEW because
  its price was missing stays REVIEW after the fill; what changes is that the
  reviewer now has a candidate value with a stated source instead of a blank.
- The UI must render filled cells differently from read cells, next to the
  existing 「未读到」 treatment.

### 4.4 Hard gate, and partial verifiability

A filled row that fails §2.4 is **discarded, not stored**. A blank is an
honest state; a number that cannot satisfy its own row arithmetic is not.

Not every identity applies to every table: 绵存 and the 徐汇 documents are
单价/合价 tables with no tax split, and any row may come back with qty missing
(§2.3). Rule: **evaluate every identity whose inputs are present, require all
of them to hold, and require at least one to have been evaluable.** A row where
nothing is checkable is not fillable and stays blank. That coverage limit must
be reported, not papered over.

### 4.5 Cost

7–11 s per page per orientation on the measured calls. Only pages with gaps.
It runs after Paddle returns, so it adds to end-to-end latency — the progress
stage vocabulary from design/27 §6 needs one more entry.

## 5. Charter questions — decided 2026-08-22

### 5.1 Intra-document mixed extraction — carve-out accepted

`.claude/rules/recognition.md` says, of the tender text-layer path:

> 允许的是文档级二选一且来源诚实标注 […] **不得把它演化成文档内混合抽取**。

A gap-fill pass is, literally, a second extractor operating on part of one
document. The argument that it is nevertheless admissible:

- what the rule bans is **choosing a parser per table by apparent complexity,
  with silent capability-probing fallback** — a routing decision that hides
  which engine produced which row;
- this is not routing. The primary path is unconditional and unchanged; the
  second pass runs only where the primary **returned nothing**, is labelled
  per field (`field_sources="llm"`), and cannot raise the tier.

**Decision (user, 2026-08-22): accepted.** The carve-out is written into
`.claude/rules/recognition.md` as an explicit exception with its four binding
conditions (empty cells only · per-field provenance · arithmetic gate ·
tier unchanged), not left to interpretation. The rule text marks it approved
and not yet built, so nobody reads it as describing existing code.

### 5.2 qwen as a permanent dependency — accepted, alternatives measured out

The same rules file records that qwen is slated for removal:

> **qwen 尚未整体删除** […] 这是唯一还依赖 qwen 视觉调用的生产分支，删除
> qwen 前必须先补上。

This design adds a second, load-bearing branch, moving qwen from "being
retired" to "structurally required".

**Decision (user, 2026-08-22): test the Paddle-only alternative first.** Done —
§2.5. Both variants fail: same-orientation single-page returns identical
blanks (3/3 runs), upright single-page returns unusable structure (0/10 rows
correct end-to-end). There is no Paddle-only path to these cells.

So qwen stays, as the gap filler. The retirement note in `recognition.md`
needs updating to say so — with two load-bearing branches instead of one, "delete
qwen" is no longer a cleanup task but a re-platforming decision.

## 6. Decisions — settled at implementation, 2026-08-23

Implemented in `apps/api/intelligence/gap_fill.py`. The four §6 questions were
decided as follows; each is pinned by a test in `apps/api/tests/test_gap_fill.py`.

1. **Orientation — lazy fan-out.** Try the angles in order and stop at the first
   whose answer passes the gate. Expected cost on the measured corpus is 1–2
   calls per affected page, not 3, and the outcome is identical because the gate
   is what selects the arm either way. A refusal (`DataInspectionFailed` on 0°)
   is a normal outcome and moves to the next angle.
2. **Checksum — filled values stay out.** The declared-total checksum is the
   independent evidence that recognition was complete; feeding fills into it
   removes exactly that independence. Fills are visible separately via
   `draft.meta["gap_fill"]` and the per-row `gap_filled` flag.
3. **Official quote — confirmation promotes it like any other REVIEW row.** The
   human is the gate the charter already relies on; a filled row arrives at that
   gate labelled, not disguised.
4. **Partial recovery — first answer only, no retry.** §2.3's run-to-run variance
   would sometimes recover a missing qty on a second call, but a retry trades
   cost for a non-guaranteed gain and needs a stop rule. Un-recovered cells stay
   blank, which is the honest state.

### 6.1 Re-measured 2026-08-23: the payoff is larger than §2.6 implied

§2.6 scoped the feature at 11 rows of 938 (1.2%) and asked whether that justified
a second engine. Measuring what those rows are *worth* answers it:

泰科龙, 89 recognised rows against 89 golden rows, compared **positionally**
(the valve corpus has duplicate specs, so key-based matching mis-attributes —
an earlier attempt at this measurement did exactly that and concluded the rows
explained only 5% of the gap):

| | |
|---|---|
| rows differing from golden | **9** — indices 59–67, all page 10 |
| their combined value | **¥247,682.78** |
| the document's total gap | **¥247,682.78 (−26.22%)** |

**Those 9 rows are 100% of the gap.** The same holds in shape elsewhere: rows
with no amounts explain ~96% of 亨通's −7.82% and ~67% of 浦东's −7.20%.

End-to-end through the implemented path, with the golden values standing in for
the model (offline, no network):

| model answer | filled | 泰科龙 total deviation |
|---|---|---|
| correct orientation | 19 rows / 64 fields | **−26.22% → +0.00%** |
| shifted orientation (tax amount in the incl-tax slot, the real 270° failure) | 10 rows / 10 fields, **9 rejected on page 10** | **−26.22%, unmoved** |

The second row is the one that matters: a well-formed wrong answer reached the
gate and **no money got through**. That reproduces §2.4's 9/9 vs 0/9 separation
on the production code path rather than in a probe.

### 6.2 One thing this does not fix

L0 from design/40 — restructuring the extraction path — was considered first and
**measured to have no effect on these numbers**; see the retraction banner at the
top of design/40. The values are absent from the engine output (5 of the 9 unit
prices occur zero times in the entire Paddle response), so no amount of
re-plumbing recovers them. Gap fill is the only mechanism that can.

## 7. Original open decisions (superseded by §6)

1. Orientation: always fan out 3 ways, or try one and fan out on failure.
2. Do filled values enter the declared-total checksum, or is the checksum
   computed on read-only values with the fill shown separately? (Leaning
   separate: the checksum is the independent evidence that recognition was
   complete, and feeding fills into it removes exactly that independence.)
3. Whether a filled row may ever reach an official quote after human
   confirmation, or is capped at preview. (Leaning: confirmation promotes it
   like any other REVIEW row — the human is the gate the charter already
   relies on.)
4. Whether to retry the same orientation on partial recovery (§2.3 run-to-run
   variance would sometimes recover seq 65/66's qty on a second call), or
   accept first-answer-only. Retrying trades cost for a non-guaranteed gain
   and needs a stop rule.

## 8. Explicitly out of scope

- 泰科龙 seq 44–49 / 82–84 (名称 read as the previous group's product) and the
  型号 column smoothing next to it. Those cells hold values. Replacing them is
  overwriting, not filling.
- Any change to alignment. The 9 bad-name rows are correctly rejected today;
  making alignment tolerate them would hide a recognition defect.
- The `merge_tables` work and the `SourceRef.page_end` fallback, both landed
  2026-08-22 — see HANDOFF.md.
