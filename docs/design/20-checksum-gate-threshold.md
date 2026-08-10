# Checksum Gate: Re-deriving the Block Threshold

> **Status — draft, 2026-08-10. Not implemented.** Written at the user's request after an
> audit found the current threshold blocks 1 of 4 real defects. The recommendation here
> changes what production refuses to store, so it needs sign-off before implementation.
>
> Scope: `CHECKSUM_BLOCK_DELTA_RATIO` in `apps/api/core/domain_config.py`, consumed by
> `_build_checksum` / `_gate_integrity` in `apps/api/services/quote_confirmation_service.py`.
> Basis: CLAUDE.md §4 (quality tiers), `.claude/rules/bid-compare-backend.md`.

## 1. What the gate is for

A bid document declares its own total. The sum of its detail lines should equal that total.
The declared total is the one number in the document that does **not** depend on how well we
extracted the detail lines, so the comparison is a genuine self-check: it needs no golden
answer, and it is available at ingestion time.

When the two disagree, either we dropped rows or we misread values. Both are defects, and
both are invisible to every other gate — a misread amount is still "a legal number", and it
still satisfies row-level arithmetic (`qty × unit_price = total`).

## 2. Where the current 0.5% came from

`HANDOFF.md:168` records a single observation: a 180° orientation misjudgment on one document
produced a **−129,532 yuan error, 0.63%** of that document's declared total. The threshold was set
to 0.5% so that this one case would be caught (the previous value, 5%, let it through).

**The methodological flaw**: that reasoning establishes only a *lower bound on sensitivity* —
"the gate must be at least this sensitive." It never asked the complementary question: *what
does this threshold let through?* A single observation cannot answer that; a distribution can.

## 3. What the threshold actually lets through

Seven documents through the production VL-direct recognizer, 2026-08-10
(`tmp/prod_e2e4`, scored against each document's own declared total):

| Document | Declared total | 0.5% permits | Observed delta | Verdict |
|---|---:|---:|---:|---|
| 宏胜 | 20,597,048 | 102,985 | +0.04 | pass |
| 远东 | 20,014,715 | 100,074 | ±0.00 | pass |
| 上海绵存 | 1,667,051 | 8,335 | ±0.00 | pass |
| 泰科龙 | 1,067,616 | 5,338 | −476 | pass |
| 亨通 | 20,966,959 | 104,835 | +1,985 | pass |
| 凯硕新正 | 932,154 | 4,661 | −2,420 | pass |
| 上海浦东 | 20,629,763 | 103,149 | −1,246,551 | **block** |

**Four documents carried real defects; the gate stopped one.** On a 20M bid, 0.5% permits a
100,000 yuan discrepancy.

### 3.1 The noise floor

When extraction is correct, the residual is not "small" — it is essentially zero: 远东 and
上海绵存 land exactly on the declared total; 宏胜 is off by 0.04 yuan, which is rounding in the
golden file itself. Measured noise is on the order of 1e-7 relative. The threshold sits at
5e-3 — roughly **25,000× above the noise it is supposed to tolerate**.

### 3.2 The shape is wrong, not just the value

A ratio threshold scales the permitted *absolute* error with the size of the document. But a
misread row costs what it costs; its absolute value does not grow because the bid is larger.
The current shape therefore gets the incentive backwards: a document with more rows has more
opportunities to err **and** receives a larger absolute allowance.

Tightening the ratio does not fix this. At 0.01%, 泰科龙's 476 yuan error blocks while 亨通's
1,985 yuan error passes — the smaller absolute error is refused and the larger one accepted,
purely because of the size of the document it sits in. That is the opposite of what a
procurement reviewer cares about.

## 4. Proposal: scale the tolerance to row count, not to money

The legitimate residual comes from per-line rounding at two decimal places. That accumulates
with the **number of lines**, not with the value of the contract.

```text
permitted delta = row_count × PER_ROW_ROUNDING_TOLERANCE      (proposed: 0.01 yuan/row)
```

Against the same seven documents:

| Document | Rows | Permitted | Observed | Result |
|---|---:|---:|---:|---|
| 宏胜 | 136 | 1.36 | 0.04 | pass |
| 远东 | 136 | 1.36 | 0.00 | pass |
| 上海绵存 | 89 | 0.89 | 0.00 | pass |
| 泰科龙 | 89 | 0.89 | 476.31 | **block** |
| 凯硕新正 | 89 | 0.89 | 2,420.00 | **block** |
| 亨通 | 135 | 1.35 | 1,985.02 | **block** |
| 上海浦东 | 137 | 1.37 | 1,246,550.93 | **block** |

4 of 4 defects blocked, 0 of 3 clean documents falsely blocked.

### 4.1 Honest limits of that table

**The rule was derived from these same seven documents.** Scoring it on the data that produced
it demonstrates nothing about generalization. What supports the proposal is the *argument* —
rounding accumulates per line, so the allowance should be denominated in lines — which stands
independently of this sample. The 4/4 result only shows the sample does not refute it.

**Orientation is a confound in the defect column.** A later single-variable A/B (orientation
held fixed, three runs per arm) reproduced 上海浦东 at **136/136 rows and ±0.00** in four of six
runs. Most of the deviation in §3 was caused by orientation misjudgment, not by inherent
extraction noise. Two consequences:

- The residual after orientation is fixed is ~0, which strengthens the case for a tight gate.
- **The gate is not the real fix.** Orientation stability is. A tight checksum converts a
  silent money error into a refusal — worth having, but it stops bad data, it does not
  produce good data.

## 5. Open question this design does not answer

The tight threshold assumes **declared total == sum of the itemised list**. If a document's
declared total legitimately includes items outside the list — taxes, discounts, provisional
sums — the gate will refuse a correct document.

Evidence available: three of seven documents match their declared total exactly when
extraction is correct, i.e. no out-of-list items in this sample. Seven documents cannot
establish a base rate.

This is deliberately left open rather than resolved by guesswork, because the existing
`checksum_ack` path already covers it: a reviewer confirms the discrepancy and the document
proceeds, with the acknowledgement recorded. The design is coherent under either answer; only
the review burden changes, and that is measurable in production in a way it is not measurable
now.

**Recommended instrumentation before switching the threshold**: log the delta and row count
for every confirmation for one production cycle without changing the gate. That yields the
real distribution, including any out-of-list documents, at zero risk.

## 6. Implementation sketch (not yet done)

1. Replace `CHECKSUM_BLOCK_DELTA_RATIO` with `CHECKSUM_PER_ROW_TOLERANCE` in
   `domain_config.py`, documenting the derivation above.
2. `_build_checksum` compares `abs(delta)` against `row_count × tolerance`. The three-state
   result (`pass` / `fail` / `unknown`) is unchanged; `unknown` must remain distinct from
   `pass` — a missing declared total is not a passed check.
3. Keep the `checksum_ack` bypass exactly as it is: an explicit human acknowledgement, recorded
   with the original values.
4. Tests: the four defect documents block, the three clean ones pass, `unknown` stays
   `unknown`, and an acknowledged failure stores with the flag intact.

## 7. What is retracted by this document

`HANDOFF.md` previously recorded that 0.5% was chosen because it "catches the 0.63% case."
That statement is accurate but incomplete, and it was used as if it justified the value.
It justifies only a ceiling. The value itself was never derived from a distribution, and on
the first distribution measured it stops 1 defect in 4.
