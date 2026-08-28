# 34 — Refusing shifted rows instead of storing wrong numbers

> **Status: implemented 2026-08-22.** §2 is measurement against all seven quote
> fixtures.
>
> Read §2.1 together with **§2.5, which corrects it**: §2.1 measured "is this
> value anywhere in the response" *per row* and concluded the shifted rows' data
> was gone. Per *column* that is only true of 数量/单价 — the 合价 usually
> survives the shift and is now recovered from an anchor. §3 changed accordingly:
> refuse everything except the total.
>
> §5's decision (row stays REVIEW) was taken and the submission-level rule was
> aligned with it — including one threshold whose calibration basis does **not**
> match this detector and is flagged there for revisit.

## 1. Trigger

design/33 §2.6 split the corpus's 80 missing-total rows into 11 genuine holes
and 69 "column shift". User, 2026-08-22:

> 先修列错位（覆盖面大 7 倍、不用新引擎、不用新依赖）

The premise behind that ranking — that the shifted rows' values are present and
merely mis-assigned, so a better per-row anchor recovers them — was checked
first, and it is false.

## 2. Measured

### 2.1 The shifted rows' true values are not in the response at all

Searching the entire 远东 Paddle response (raw JSON, full text) for each value
in the reference quote list:

| Field | Reference rows | Value absent from the whole response |
|---|---|---|
| 数量 | 136 | **28** |
| 单价 | 136 | **28** |
| 合价 | 136 | 6 |

seq 3 is typical: the reference says `数量 2987.24 / 单价 557.71`, and neither
string occurs anywhere in the response. The engine did not misplace them — it
never produced them.

**Consequence — but only for these two columns, see §2.5:** no re-anchoring
scheme can recover 数量/单价 for these rows. The tax-rate
anchor (`_locate_tax_rate_idx`) that handles the 报价 side's row-level cell drops
would not have helped even if the 徐汇 documents had a tax column. Recovery
requires re-reading the page, which is design/33's territory.

### 2.2 The real defect: we store wrong numbers, unflagged

远东 page 3, as currently extracted:

| seq | stored 数量 | true 数量 | stored 单价 | true 单价 | flags |
|---|---|---|---|---|---|
| 1 | 1905.25 | 1905.25 | 873.57 | 873.57 | `[]` |
| 2 | 2882.94 | 2882.94 | 692.92 | 692.92 | `[]` |
| 3 | **150009.49** | 2987.24 | **1666013.63** | 557.71 | `[]` |
| 4 | **150009.49** | 207.52 | **150009.49** | 593.36 | `[]` |
| 5 | **150009.49** | 640.30 | **150009.49** | 234.28 | `[]` |

**31 of 远东's 138 rows carry a wrong 数量, with no validation flag at all.**
These are not blanks. They are plausible numbers in the right shape, stored as
if read, and nothing downstream knows otherwise. Under CLAUDE.md §4 a blank is
an honest state and a flagged doubt is a manageable one; a silently wrong
quantity in a price comparison is neither.

This is a more serious problem than the missing values in §2.1, and it is the
one worth fixing first.

### 2.3 Mechanism, and why the existing guard misses it

Raw trailing cells for the same rows (header: … `单位 数量 单价 合价 备注`):

```
seq 1  ['米', '1905.25', '873.57',    '1664369.25', '*GB-WDZA-…-3*240+2*120']
seq 3  ['米', '150009.49', '1666013.63', '*GB-WDZA-…-3*150+2*70', '']
```

The row lost one cell, so everything after 单位 slid left by one. The 备注 free
text lands in the **`total_price` slot**, and the tail is padded empty.

Then `classify_amount_cell` sees non-numeric text in an amount slot and returns
`AMOUNT_EMPTY`, so `total_price` becomes `None` — **the one cell that proves the
row is broken is silently discarded**, while the two shifted numbers beside it
survive looking perfectly normal.

Two existing guards do not catch this:

- `check_column_alignment` (→ `column_shift` flag) runs in `parse_csv`, on the
  canonical CSV, where every row is already canonical width. The shift happened
  upstream in the Paddle matrix and is invisible by then.
- `_has_plausible_numeric_signal` requires *at least one* numeric slot to parse.
  Here `qty` and `unit_price` parse fine, so the row passes.

### 2.4 The detector is free and general

Rule: **a slot that must be numeric holding text that is neither a number nor a
not-quoted marker (`/`, `无`) means this row's positional mapping is broken.**
No new threshold, no document-specific keyword, and it reuses
`classify_amount_cell`'s existing vocabulary.

Rows it fires on, per document:

| Document | Data rows | Rows with free text in a numeric slot |
|---|---|---|
| 泰科龙 | 189 | 10 |
| 凯硕 | 113 | 2 |
| 绵存 | 125 | 17 |
| 亨通 | 143 | 6 |
| 宏胜 | 160 | 9 |
| 浦东 | 282 | 4 |
| 远东 | 144 | **33** |
| **total** | — | **81** |

It fires on every document in the corpus, which is the point: this is not a
远东 patch.

### 2.5 Correction, measured per column: the total is recoverable, quantity and unit price are not

§2.1 searched the whole response for each reference value and concluded the
shifted rows' data was gone. That was measured **per row**, and it is wrong at
**column** granularity. Re-measuring one column at a time:

| Document | reference rows | total already extracted | total present in raw JSON but not extracted | total absent entirely |
|---|---|---|---|---|
| 泰科龙 | 89 | 80 | 1 | 8 |
| 亨通 | 136 | 118 | 4 | 14 |
| 远东 | 137 | 96 | **35** | 6 |

远东's 数量/单价 really are absent (28 rows, §2.1 stands for those columns), but
its **合价 is present in 35 rows** — sitting one slot to the left, in the
`unit_price` position, because the row shifted.

Why the asymmetry: a shift means one cell was **dropped**. Everything to the
right of the drop point is displaced but intact; everything to the left is
untouched; only the dropped cell itself is gone. 合价 sits next to 备注, on the
right of the drop point — so it survives. 数量/单价 sit at or near the drop
point, and re-shifting them yields a neighbour row's value contaminated by the
vertical run-length smearing (verified: one row re-shifts to a correct
`1666013.63` total but a bogus `150009.49` unit price, which is another row's
total).

**So the rule is per column, not per row**: recover the total, refuse the rest.

## 3. Proposal

When a row trips §2.4:

1. **Do not store any positionally-mapped numeric field for that row** — qty,
   all unit prices, all totals, tax amount. They are all downstream of the same
   broken offset; keeping the ones that happen to parse is exactly the current
   bug. (Two narrow exceptions, both evidence-backed: the row's own arithmetic
   still closes — then the money fields are kept, which saves the "unit fell
   into the qty slot" rows; and the total, which is re-derived from the anchor
   below.)
2. Keep name / spec / seq. Those sit **before** the shift point (the drop is
   always in or after the quantity region in every observed case) and are what
   the reviewer needs to identify the row.
3. Flag the row `column_shift` — the flag value already exists in the
   vocabulary, so the doubt inbox and quality gate need no new term.
4. Preserve the raw cells as they came back. The row must remain diagnosable
   without re-running recognition.

**Recover only the total; refuse a corrected mapping for anything else** (§2.5),
and recover it **only in the single configuration where the reasoning holds**:

- **the free text sits immediately left of the table's rightmost column**, i.e.
  the shift is exactly 1. That is the 备注-slid-one-left shape, and it is the
  only one where "the value now in the total column's left neighbour is the
  total" follows from anything;
- **exactly one dirty slot**, otherwise the row is disordered in more than one
  place;
- **the source cell must differ from the previous row's same cell** — an
  identical value in the same position on consecutive rows is the run-length
  smearing signature, and recovering a contaminated total is worse than leaving
  it blank (a blank is visible, a wrong number is not).

**The first guard was added after the generality audit and it matters.** The
first implementation computed the shift as "distance to the nearest non-amount
column on the right", justified as «备注 is the only free-text column». That
premise is false, and the audit showed it: `tax_rate` is deliberately outside
`_AMOUNT_SLOTS` (its legal value "13%" does not parse as a number) so it gets
picked as a text "home"; `brand` and `remark` are both free text with nothing to
choose between them; on one document the computed shift came out as 3, which no
single dropped cell can produce. That version scored 26/29 **partly by luck**.

Measured effect of the tightening: recoveries drop from 29 to 22, and **not one
document loses a single correct total** (泰科龙 stays at 80 hits, 凯硕 at 87) —
the seven recoveries removed were on rows whose money had *already* been kept by
the arithmetic guard, so they were redundant work resting on invalid reasoning.
Same accuracy, far fewer unjustified guesses.

Remaining outcome: **22 recovered, 20 correct, 2 wrong** (both 远东), plus one
pre-existing wrong recovery on 亨通 that the shape guard does not catch. Wrong
recoveries carry both `column_shift` and `total_recovered_by_shift`, so they are
visible, excluded from official totals, and never silently trusted. This is the
one place in this design where a wrong value can be introduced; it is bounded,
flagged, and measured, and its error rate (≈3/23) is **not** something this
corpus can prove will hold elsewhere — see §8.

## 4. What this changes downstream

- 远东: 31 rows go from *wrong quantity* to *blank quantity + flag*. Line-sum
  totals will move, and the declared-total checksum will move with them. That
  is the gate doing its job on data it previously could not see.
- The rows become `AMOUNT_EMPTY`, i.e. exactly design/33's gap-fill population.
  **The two designs compose**: 34 turns silent corruption into honest gaps; 33
  then recovers what it can and leaves the rest blank. Sequenced 34 → 33, 33's
  addressable population grows from 11 rows to roughly 11 + 81.
- Existing snapshot/replay expectations that assert row-level values for these
  documents will need re-recording, with the diff explained rather than the
  expectation loosened.

## 5. Decision needed

**Does a `column_shift` row stay REVIEW, or become BLOCKED?**

- REVIEW keeps it visible and human-fixable, consistent with how every other
  row-level doubt is handled, and lets design/33 later fill it.
- BLOCKED matches «key amount conflict» in CLAUDE.md §4 more literally.

**Decision (user, 2026-08-22): REVIEW.**

Implementing it exposed a second, submission-level rule that contradicted it:
`submission_eligibility.py` blocked the **whole submission** on any single
shifted row. That rule predates the recognition-side detector, when
`column_shift` essentially never fired; with the detector in place four of seven
documents tripped it, two of them because of a single row.

It is now aligned with the ratio policy that `domain_config` **already
documented** («低于此比例仍逐行 BLOCKED 那些行本身，只是不牵连整份») but that the
submission-level code never implemented — reusing
`INTEGRITY_COLUMN_SHIFT_BLOCKED_RATIO` (2%) and
`INTEGRITY_COLUMN_SHIFT_BLOCKED_COUNT` (3) rather than inventing a threshold.

| Document | detail rows | shifted | ratio | verdict |
|---|---|---|---|---|
| 绵存 / 宏胜 / 浦东 | 89 / 136 / 264 | 0 | 0% | usable |
| 亨通 | 132 | 1 | 0.8% | usable (row in review) |
| 凯硕 | 89 | 1 | 1.1% | usable (row in review) |
| 泰科龙 | 89 | 6 | 6.7% | **BLOCKED** |
| 远东 | 138 | 32 | 23.2% | **BLOCKED** |

**Open question on the threshold, flagged deliberately**: those two constants
were calibrated against a *different* detector (rows whose cell count ≠ header
width, measured on a batch where a shift affected 86/90 rows). The new detector
is type-based and fires on a much narrower population. Reusing the number avoids
inventing one, but it is **not** evidence that 2% is right for this detector —
泰科龙 is blocked at 6.7% while its recovered totals are 80/80 correct against
the reference, which is arguably too strict. Revisit with data before treating
2% as settled.

## 6. Out of scope

- Recovering the lost values — design/33.
- 泰科龙's 名称/型号 run-length smearing. Those cells hold wrong *text*, not
  wrong numbers, and no arithmetic or type signal exposes them. Undetected and
  still unsolved.

## 8. Generality — what is and is not established

Asked directly during review: do these fixes generalise? Honest answer, rule by
rule.

**Established.** The detector (`_dirty_amount_slots`) is a type judgement —
"a slot that must hold a number holds free text" — built on vocabulary that
already existed, with no document-specific constant. It fires on all seven
fixtures (2–33 rows each). This one is defensible.

**Structurally biased, and the bias is measured.** The arithmetic guard that
decides how much of a shifted row to blank keeps money on tax-bearing tables
(7 of 12 rows, 金桥) and never on tables without a tax column (0 of 69, 徐汇 +
绵存) — those have only `qty × unit = total`, and the dirty slot is usually
`qty`, so nothing is evaluable and the rule degenerates into "blank everything".
Current cost on this corpus is 2 rows; on unseen 单价/合价 documents it is
unbounded. A second, deeper limit: arithmetic validates the trio *internally*,
not its attachment to the row's identity — a wholly displaced row could be
self-consistent and still belong to the neighbouring item.

**Corrected during the audit** — the anchor rule, see §3 above. Worth recording
that the original version passed every test and matched the reference 26/29
times while resting on a false premise. Test-passing was not evidence of
correctness here; the probe that printed *which column it treated as the anchor*
was.

**The limit that covers all of the above:** every rule in this document was
derived from and validated on the same seven documents. **There is no held-out
document.** Type-based judgements generalise better than tuned constants, and
these are mostly type-based — but "it fires sensibly on all seven" is not
evidence about the eighth. Before any of this is described as general, it needs
a document nobody looked at while writing the rules.
