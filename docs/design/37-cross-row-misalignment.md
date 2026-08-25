# 37 — Cross-row misalignment: the whole numeric row lands on the wrong name

> **Status: proposal, nothing built.** Every number in §2–§4 was measured on the
> committed Paddle snapshots on 2026-08-23 and is reproducible by replay.
> §6 has the decisions that need an explicit answer.

## 1. Trigger

Manual testing surfaced 亨通 showing 82/135 priced rows where recognition had
read 125 unit prices. Chasing that down produced a defect class none of the
existing gates can see.

## 2. What it looks like

亨通's quote, replayed from `quote_hengtong.json`, compared against the
supplier's own reference CSV:

| 规格 | 识别合价 | 标答合价 | 那个数其实是谁的 |
|---|---|---|---|
| WDZA-YJY-4\*70+E35 | 329,948.25 | 298,651.22 | 上一行 (3\*95+2\*50) |
| WDZA-YJY-3\*70+2\*35 | 298,651.22 | 244,848.86 | 上一行 (4\*70+E35) |
| WDZA-YJY-4\*50+E25 | 244,848.86 | 411,265.24 | 上一行 (3\*70+2\*35) |
| WDZA-YJY-4\*35+E16 | 411,265.24 | 174,192.5 | 上一行 (4\*50+E25) |

A chain: each row carries the **previous** row's money. The numbers are all
real numbers from the document, all of plausible magnitude, all in the right
column. Only the name they hang on is wrong.

Measured across the four 徐汇 quotes (`value_audit()` in
`test_scenarios_e2e.py`, counting only rows whose 品名+规格 is unique on both
sides so a mismatch cannot be a matching artifact):

| | 可判定 | 值正确 | 空 | 值错·有 flag | **值错·无 flag** |
|---|---|---|---|---|---|
| 宏胜 | 78 | 78 | 0 | 0 | 0 |
| 远东 | 113 | 92 | 10 | 5 | 6 |
| 亨通 | 81 | 63 | 5 | 1 | **12** |
| 浦东 | 72 | 57 | 8 | 0 | **7** |

## 3. Why nothing catches it

### 3.1 design/34's `column_shift` is the wrong axis

design/34 detects **horizontal** damage: free text landing one column left of
the rightmost column inside a single row. This is **vertical**: the column
mapping within each row is perfectly fine, the whole numeric block is offset
against the text block by one row. 亨通's `column_shift` count is 1 — the
detector is working as designed and this is simply not what it looks for.

### 3.2 The arithmetic gate is structurally blind here

`数量 × 单价 = 合价` is the cheapest identity available, and it **holds**:

| 规格 | 数量 | 单价 | 合价 | 数量×单价 |
|---|---|---|---|---|
| WDZA-YJY-4\*70+E35 | 1074.47 | 307.08 | 329,948.25 | 329,948.25 ✓ |
| WDZA-YJY-4\*50+E25 | 1089.04 | 224.83 | 244,848.86 | 244,848.86 ✓ |
| WDZA-YJY-4\*35+E16 | 2335.54 | 176.09 | 411,265.24 | 411,265.24 ✓ |

**18 of the 25 silently-wrong rows across three suppliers close arithmetically.**
Quantity, unit price and total moved *together*, so the row is internally
consistent — it is only externally misattached. No within-row identity can see
that, no matter how many we add.

### 3.3 The aggregate baselines cannot see it either

After the shift, 亨通 still measures: `rows=132`, `blank_total=10`,
`column_shift=1`, `total_delta_pct=-7.82%`. All four numbers sit inside their
tolerances. Before 2026-08-23 those were the only stage-one assertions, so the
suite was green on a document with 12 wrong amounts.

This is now covered: `test_silent_wrong_amounts_do_not_grow` and
`test_row_value_accuracy_within_baseline` pin the per-row figures with zero
tolerance on `silent`. **That is a regression guard, not a fix** — it freezes
today's damage in place and makes it louder, nothing more.

## 4. What can see it: cross-supplier quantity voting

Quantities are not the supplier's to choose. They come from the tender, so
every supplier bidding the same list must report the same quantity for the same
item — the reference lists already assert this
(`test_quantities_are_identical_across_suppliers`), and design/32 leans on it
to build the quote-derived axis.

So a supplier whose quantity for a given 品名+规格 disagrees with the majority
of the other suppliers has something misattached on that row.

Measured, using the other three suppliers as voters and requiring ≥2 agreeing
votes:

| | 静默错行 | **抓到** | 漏 |
|---|---|---|---|
| 亨通 | 12 | **10** | 2 |
| 浦东 | 7 | **6** | 1 |

16 of 19. The three misses are a **different** defect: the quantity is right and
only the amount is wrong — 浦东's `WDZA-YJY-3*95+2*50` reads 33,913.04 where the
truth is 333,913.04, a dropped leading digit. Quantity voting cannot see that
one and should not be asked to.

### 4.1 The architectural consequence

This check **cannot live in single-document recognition.** It needs the other
suppliers, which means it belongs at alignment/preview time, not in
`paddle_vl.py`. That is a real cost: today all structural doubts are raised
during recognition and travel with the draft, and this one would have to be
raised later, against an assembled set.

It also does not exist for the first supplier uploaded, or for a project where
only one supplier bids. The flag it raises is therefore **conditional on having
peers** — the message has to say so rather than implying the row is clean when
there was simply nobody to compare against.

## 5. What this document does not fix

- **The dropped-digit class** (§4 misses). Different cause, different detector;
  not addressed here.
- **The name-column smearing** of design/34 §6 — `矿物电缆矿物电缆`,
  `普通电缆普通电缆普通电缆`, `采购文件` leaking into the 品名 column. It has the
  same *symptom* (numbers attached to a wrong name) but its cause is the name
  column, not the numeric block, and it is already recorded as unfixed.
- **Repairing** a detected misalignment. This proposal only detects. Shifting
  the numbers back is a guess about which direction and how far, and CLAUDE.md
  forbids raising a tier by silent correction. Detected rows go to REVIEW with
  the peer evidence attached; a human decides.

## 6. Decisions needed

1. **Where does the check run** — a new pass inside the preview/alignment
   service, or an extension of `draft_integrity` that takes the peer set as an
   argument? The second keeps all structural doubts in one place but makes
   `draft_integrity` no longer a per-document function.
2. **Vote threshold.** Measured with "≥2 agreeing peers". With only 2 suppliers
   total there is exactly 1 voter and no majority — do we flag on a single
   disagreeing peer (noisier, but the alternative is no coverage at all for
   2-supplier projects), or stay silent and say so?
3. **What tier does a flagged row get.** REVIEW keeps it out of official
   totals and recommendations, which is correct — but 亨通 would take 10 rows
   to REVIEW on one document, and the preview lane must still not block
   (design/36). Confirm REVIEW-with-advisory-gate is the intended behaviour.
4. **Does the flag survive into exports.** A row whose amount disagrees with
   peers is exactly the row a human reviewer wants to see in the exported
   matrix, but no export field carries per-cell doubt today.
