# 19 — Recognition Robustness

> Status: **Draft** · Created: 2026-08-09 · Author: system
> Supersedes the orientation-only draft of the same number.
> Evidence: `tmp/api_e2e_cable/` — 4 real scanned bid PDFs, 徐汇华泾 cable round, 2026-08-09.

## 1. Goal and the standard being applied

Every defect this round exposed is **silent**: the system produced a confident
answer while losing or corrupting data, and nothing downstream could tell. Half a
tender list vanished, one bidder's columns were scrambled, another lost 14 of 19
pages — and every API call returned 200.

So the target is not "higher accuracy". It is:

> **No silent failure.** Every row that should exist is either extracted, or
> explicitly accounted for as lost with a reason. Every value that cannot be
> trusted is flagged before it reaches comparison.

Accuracy improvements follow from this; they cannot be measured before it.

## 2. What this round exposed

| # | Failure | Layer | Silent? | Evidence |
|---|---|---|---|---|
| 1 | Visual classifier reports `orientation: 0` for every page, including 90° and 180° ones | page | yes | probed 远东 p3/4/5, 宏胜 p3/5/6 — all `0` |
| 2 | `_ORIENT_ROTATIONS = (90, 270)` — 180° never a candidate | page | yes | `table_recognizer.py:948` |
| 3 | Chain early-exit marks the whole chain upright when half its pages score | page | yes | 远东 p3/8/9 scored, other 14 pages never corrected |
| 4 | Column-coverage scoring cannot detect 180° (reversal preserves the column set) | page | yes | 宏胜 p3 scored well, emitted 22 scrambled rows |
| 5 | Column scramble produces plausible numbers | extract | yes | 宏胜 `合价 1987156.70` → `qty 1987.1567` |
| 6 | `parse_tender_xlsx` reads `wb.active` only | input | yes | 184 rows in, 92 out; 矿物电缆 sheet dropped |
| 7 | Rows with empty 序号 (continuation rows) skipped | input | yes | 14 rows across both sheets |
| 8 | `seq` is an identity key, so multi-sheet merge would overwrite anchors | input | yes | `bid_matrix.py:571`, `supplier_fill_llm.py:213` |
| 9 | OCR inserts spaces inside model codes | extract | yes | `RTTY Z-3*240`, `Y FD-WDZA-Y JY` — 78% of 亨通 rows |
| 10 | Cover-page supplier name empty | extract | no | 4/4 blank, caller fell back to filename |
| 11 | Arithmetic check runs only at match time, after storage | gate | — | quality gate blocked, but data already written |
| 12 | Text pre-filter yields nothing on pure scans | perf | no | all 4 docs have zero text layer |

Single-page proof that #1–#4 are the dominant cause of row loss:

```
远东 p4 as-is     OCR 1649 chars   0 usable rows
远东 p4 rotated    OCR 2865 chars   13 rows, arithmetic exact (148.54×934.47=138806.18)
```

## 3. Design

Five layers. Each one either fixes a silent failure or converts it into a visible one.

### L0 — Input completeness (tender side)

**Multi-sheet.** Parse every worksheet, not `wb.active`. Blocked today by `seq`
being an identity key: two sheets both numbered 1..N would collide and silently
overwrite in the matrix. Introduce a **composite anchor key** `(sheet_ordinal, seq)`
carried through `TenderAnchor`, with `seq` retained verbatim for display and
traceability. Every consumer that currently keys on `str(seq)` / `int(seq)` moves
to the composite key.

**Continuation rows.** Rows with an empty 序号 but a non-empty name are real
material lines (a parent model with sub-specs). Keep them, inheriting the parent's
composite key with a sub-ordinal. Never drop silently.

**Ledger.** `parse_tender_xlsx` returns, alongside anchors, a per-sheet accounting:
rows seen / rows accepted / rows rejected with reason. A parse that drops rows
without recording why is a defect.

### L1 — Page orientation, four-way, two independent paths

Both paths get fixed; neither is trusted alone.

**Path A — the visual classifier (now in scope to fix).** Its orientation output is
currently useless. Three concrete suspects, to be tested against a labeled set
before changing anything:

- thumbnails are rendered at `scale=1` — possibly too low to judge glyph direction
- the prompt states the allowed values but gives no decision procedure
- batching 10 pages per call may dilute per-page attention

The fix is measurable because this round produced ground truth: **56 pages across
four documents with known orientation** (远东 19 @ 90°, 宏胜 11 @ 180°, 亨通 11 @
90°, 上海浦东 15 @ ~0°). Label them once, then iterate the prompt against a pass
rate. Classification runs on thumbnails, so iteration is cheap.

Target: ≥90% exact-angle agreement on the labeled set. Below that the classifier
stays a hint.

**Path B — probe-based detection, all four angles.** Candidate set becomes
`(90, 180, 270)`, probed lazily so the upright case still costs nothing:

```
0° passes coverage AND arithmetic   -> no probe
coverage poor                       -> wrong axis    -> probe 90, 270
coverage good but arithmetic fails  -> flipped/scrambled -> probe 180
```

**Per-page stragglers are re-probed.** The chain angle remains a cheap first guess;
it stops being the final word. Any page still failing after the chain angle is
applied gets its own probe. This removes the majority early-exit (#3) and handles
documents with mixed page orientation (远东 p19 is landscape, the rest portrait).

Cost is accepted: on a 19-page document with 4 sample pages, the flipped path adds
roughly 4 OCR calls (~15s). Measure it in the fresh E2E; optimize only if it hurts.

**Rejected — free image-based axis detection.** A projection-profile heuristic (ink
differenced along rows vs columns) was measured on the real pages and does not
separate the axis; table ruling lines dominate the ink:

```
远东 p3 (90°)  rowProj 35.82  colProj 35.52  -> misclassified
亨通 p3 (90°)  rowProj 30.39  colProj 44.21  -> correct
```

Recorded so it is not re-attempted.

### L2 — Extraction integrity

**Arithmetic consistency becomes an extraction-time signal.** `qty × unit_price ≈
total_price` over the parsed `TableGrid`, computed with zero extra API cost. It is
the only signal that detects 180° and column scrambling, because the column *set*
survives reversal while the *values* do not.

The same check already exists in the match-time quality gate; this moves it early
enough to act on rather than merely to reject. Orientation scoring becomes the pair
`(column_coverage, arith_ok)` compared lexicographically — an angle wins only if it
does not lose arithmetic. Quote documents only; tender lists have no price columns
and keep coverage-only scoring.

**Model-code normalization.** OCR splits letter runs in model strings
(`RTTYZ`→`RTTY Z`, `YJY`→`Y JY`). Add a deterministic normalized form used for
matching and comparison, with the raw string preserved for display and audit.
Affects 78% of rows in the best-performing document, and model codes are the *only*
matching signal for cable (there are no distinguishing Chinese product names).

### L3 — Row conservation ledger

The defect that makes everything else dangerous: nothing tracks how many rows
*should* exist. Introduce an explicit ledger per submission:

```
expected (anchors)  ->  recognized  ->  confirmed  ->  aligned
```

Every drop between two adjacent stages carries a page number and a reason. A stage
that cannot explain its delta fails the quality gate rather than passing quietly.
This is what would have caught #6, #7, and the 14 empty pages of 远东 on the spot.

### L4 — Provenance

Record per page: `rotation_applied`, the score pair that justified it, whether the
angle came from the classifier or a probe, and — for pages that yielded nothing —
why. A silently rotated page is as unauditable as a silently dropped one.

## 4. Order of work

Sequenced by "how much does the next measurement depend on this".

| # | Item | Layer | Why this position |
|---|---|---|---|
| 1 | Arithmetic signal + four-way lazy probe + per-page stragglers | L1/L2 | Largest measurable recall gain; unblocks every later measurement |
| 2 | Row conservation ledger | L3 | Without it, item 1's result cannot be trusted or even stated |
| 3 | Model-code normalization | L2 | Matching is meaningless until rows exist and codes compare |
| 4 | Classifier orientation repair against the labeled set | L1 | Independent; makes item 1 cheaper by cutting probes |
| 5 | Multi-sheet + composite anchor key + continuation rows | L0 | Ripples into matrix and LLM fill — do it when items 1–3 are stable |
| 6 | Provenance fields | L4 | Cheap, but only meaningful once the above produce decisions worth recording |

## 5. Acceptance baselines

**Corrected 2026-08-09.** The earlier "182 expected rows" figure was wrong — it came
from the tender Excel (90 mineral + 94 ordinary cable rows), but the *bid* forms carry
**136 line items**. The customer supplied per-bidder reference transcriptions, audited
and version-controlled as `data/golden/quote_cable_*.json` (all four `audit_status=clean`).

| Doc | Rows (v3) | Reference | Declared total | Notes |
|---|---|---|---|---|
| 亨通 | 132 | 136 | 20,966,959.43 | item 112 quoted as "/" → price and total blank |
| 上海浦东 | 246 | 136 | 20,629,762.68 | two duplicate copies of the quote table extracted |
| 宏胜 | 69 | 136 | 20,597,048.33 | line sum exceeds declared by 0.04 (rounding) |
| 远东 | 29 | 136 | 20,014,715.08 | 11 of 14 list pages produced nothing |

Acceptance is the seven cases in `apps/api/tests/test_cable_golden.py`: golden
trustworthiness (1–3, passing) and recognition against golden (4–7, pending items
L1/L4 below). Case 7 — zero silently derived totals — exists because a derived total
makes the arithmetic check pass trivially and launders column-shift errors.

**Cross-bidder pricing-basis divergence.** Item 114 carries an identical spec
(`WDZA-YJY-2*(4*240+E120)`) and quantity across all four bidders, but 上海浦东 priced
one run (884.75, implied multiplier 2.0) while the others priced the doubled cable
(~1700, multiplier 1.0). Comparing unit prices directly would rank 上海浦东 as half
price. The multiplier is a **quoting choice, not a property of the spec** — it must be
observed and surfaced for human confirmation, never inferred and "corrected".

Evidence types stay separate when reporting: unit contracts · snapshot replay
(determinism) · fresh E2E (real model chain). None substitutes for another.

## 6. Not in scope

- Arbitrary skew (scan tilt of a few degrees). Four right angles only.
- Replacing the OCR or extraction model. The single-page proof shows the current
  model is accurate once the page is upright; this is a plumbing problem.
- bbox back-fill for rotated pages (existing gap, tracked separately).
- Clarification/amendment documents (询标疑问) that modify the tender list — a real
  business gap found the same day, but it is a domain-model change, not recognition.
