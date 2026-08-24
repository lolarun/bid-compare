# 39 — Deterministic alignment when the supplier quotes only part of the list

> **Status: implemented 2026-08-23.** Every number below was measured on the
> committed fixtures and is reproducible by `pytest apps/api/tests/test_scenarios_e2e.py`.
> §7 has the one question this document deliberately leaves open.

## 1. Trigger

> 我希望这两个测试用例能够100%对齐，如果这都不能100%对齐的话，那说明有很大问题。

The two cases are the **zero-model** ones — A3 (金桥, 采购清单 xlsx + 3 报价 xlsx)
and B3 (徐汇, 采购清单 xlsx + 4 报价 csv). No recognition, no embeddings, no LLM:
deterministic parsing plus deterministic alignment, end to end. The user is right
that anything short of 100% here is a code defect, because there is no model whose
inaccuracy could absorb the blame.

Measured before this work:

| | 行轴 | 每家报价 | 对齐上 | 状态 |
|---|---|---|---|---|
| A3 | 89 | 89 | — | **整份被拒**（422 `systematic_vat_mismatch`），测试 skip |
| B3 | 92 | 136 | 79 (58%) | 22 pending / 23 聚合 |

After:

| | 行轴 | 每家报价 | 对齐上 | pending | residue | low_conf |
|---|---|---|---|---|---|---|
| A3 | 89 | 89 | **89** | 0 | 0 | 0 |
| B3 | **170** | 136 | **136** | 0 | 0 | 0 |

Three independent defects were in the way. None of them was "matching is hard".

## 2. Defect 1 — three price columns had no field to land in

`金桥地体上盖项目-泰科龙报价清单.xlsx` prints
`单价(不含税) / 合计(不含税) / 税率 / 税额 / 价税合计`. It has **no incl-tax unit
price column**.

`_TABULAR_COLUMN_PATTERNS` had exactly one tax-basis slot (`unit_price_excl_tax`).
`合计(不含税)`, `税率` and `税额` matched no slot and were **read from the file and
discarded**; `价税合计` was claimed by the generic `total_price`.

The result: `unit_price_excl_tax = 69.12` was the only unit price, and it got
paired with `total_price = 78.1` (incl-tax). Deviation 13% on every row →
`tax_basis_suspect` ×89 → `systematic_vat_mismatch` → the whole submission
refused at `/tender-list/match`.

The arithmetic layer was never wrong about this. `draft_integrity._PRICE_PAIRS`
already pairs strictly within a tax basis and its comment already names this exact
failure — 「拿不含税单价去对含税合价，会把每一行都判成错，且偏差恰好是税率——
看起来像"系统性错误"，其实是比错了尺子」. It simply never received the excl-tax
total, so its first pair could never fire.

**Fix.** Give every tax basis its own slot on both sides:
`unit_price_incl_tax` / `unit_price_excl_tax` / `total_price_incl_tax` /
`total_price_excl_tax` / `tax_rate` / `tax_amount`, with the generic
`unit_price` / `total_price` demoted to their real meaning — *the single price
column of a file that does not distinguish tax basis* (绵存's sheet).

Effective prices are **unchanged** by this, which is the point: 泰科龙 78.1,
凯硕 71.0, 绵存 93.0 before and after. `derive_price_basis` now sees both bases,
returns `dual_tax`, and takes incl — the same number the generic slot used to
carry. What changed is that the arithmetic check can finally pair like with like:
89/89 `ok` for all three suppliers, where 泰科龙 was previously 89/89
`not_evaluable`.

`derive_price_basis`'s own docstring had anticipated this file —
「如泰科龙只印含税合价+不含税单价」— and its recovery branch was written for it.
The data just never arrived.

**Also collapsed:** `paddle_vl._parse_rate` and the new Excel tax-rate read were
about to be two implementations of "13% / 13 / 0.13 → 0.13". They are now one,
`core.utils.parse_rate`. The old docstring explicitly left this open
(「另行核实是否也该在别处修」); the answer is yes.

## 3. Defect 2 — half the procurement list was never parsed

`徐汇区华泾镇项目-采购清单.xlsx` is **two sheets**: `矿物电缆` (78 items) and
`普通电缆` (92). `/tender-list/preview` called `pick_default_sheet`, which returns
the sheet with the most data rows — 普通电缆 — and parsed only that.

The row axis was therefore 92, and the 44 mineral-cable rows in every supplier's
quote had nowhere to land. Nothing anywhere said a sheet had been dropped; the
card read 「采购清单 92 项」.

**Fix.** `parse_tender_all_sheets()` merges every list-like sheet in workbook
order, renumbering `seq` to 1..N (the sequential gate requires continuous unique
sequence numbers) and keeping the original in `raw["原表序号"]`.

Two details that are load-bearing:

- **The sheet name goes into `profession`.** This list's columns are only
  `序号/名称/单位/数量` — there is no spec column, and the material category
  (矿物电缆 / 普通电缆) exists *nowhere but the sheet name*. Dropping it merges
  two tables into one indistinguishable heap. It does not overwrite an existing
  `专业` column (金桥's sheet has one).
- **Summary sheets are excluded.** "Has a recognizable header" ≠ "is part of the
  list". A real attachment often carries a 汇总表 whose rows are aggregates
  (`材料设备合计`). Merging it invents an anchor that does not exist. The
  discriminator reuses the existing `_FOOTER_MARKERS` rather than inventing a
  second keyword list: a candidate sheet whose item names are **all** totals-like
  is a summary. "All", not "most" — a real list may legitimately contain an item
  named 合计管件, and losing a whole sheet costs far more than keeping one
  spurious row.

## 4. Defect 3 — "quoted a subset" was treated as "unalignable"

Sequential direct-connect's first whole-table gate was `行数 == 锚点数`.

Suppliers quoting only part of the list is **normal business**, not corruption.
All four 徐汇 suppliers quote 136 of the merged 170 — precisely
`矿物电缆 1..44` + `普通电缆` in full. The remaining 34 are one contiguous block
of `RTXMY-*` at the tail of the mineral sheet, quantity 2 each, **quoted by
nobody**. Verified row by row against all four quote lists, which agree exactly.

With the count gate failing, every row fell through to semantic matching: 79/136.

### 4.1 The rule

Quantities are not the supplier's to choose. They come from the tender, so the
quote's quantity sequence must be an **order-preserving subsequence** of the
anchor quantity sequence. That is strong evidence and it is deterministic.

`_subsequence_positions()`:

1. **Left-greedy** (each quote row takes the *earliest* feasible anchor) and
   **right-greedy** (the *latest*).
2. Where the two agree, the position is **forced** — accept it.
3. Where they differ, the feasible window is `[left, right]`; score the candidates
   in it by normalized string similarity and require the winner to lead by
   `SUBSEQ_TIEBREAK_MARGIN`.
4. **Any row that cannot be decided aborts the whole submission** back to semantic
   matching. Better to fall back wholesale than to let one "probably this one"
   into a result labelled deterministic.

Measured on 徐汇: 135 of 136 rows agree between the two directions unaided. Exactly
one needs the tiebreak — the quote's `预分支电缆头 RTTYZ-4x120+E70-…` has quantity
2, and so does anchor `RTXMY-6*50+E25`; quantity cannot separate them, text
separates them at a glance. After the tiebreak all four suppliers match the
hand-verified ground truth row for row.

**Text similarity may never create a match.** It only ranks candidates that
quantity has already admitted. That boundary is deliberate: the moment text can
decide alone, semantic matching has been smuggled into the path that calls itself
deterministic.

### 4.2 Anchor quantity 0 means "no quantity stated", not "zero"

The mineral sheet has composite rows: a parent (numbered) with a blank or `0`
quantity, followed by **continuation rows with an empty 序号** carrying the
decomposed quantities. `parse_tender_xlsx` skips rows without a 序号 — correct for
row identity, but it leaves the parent reading 0.

The suppliers quote the parent at a value derived from the children by the number
of parallel runs:

| 父行 | 表内 | 续行 | 报价 |
|---|---|---|---|
| `WDZA-YJY-2*(4*240+E120)` | 0 | 276 | 138.09 = 276 ÷ 2 |
| `WDZA-YJY-4(1*400)+E(1*400)` | 0 | 2220 | 444.59 = 2220 ÷ 5 |
| `RTTYZ-6*150+E70` | 0 | 148 + 148 | 148.54 |
| `RTTYZ-6*50+E25` | 24.79 | 24 + 24 | 24.79 (父行本来就有值) |

Deriving the divisor requires understanding that `6*150+E70` is two runs of
`3*150` and that `4(1*400)` is five conductors — cable-spec algebra. **This
document does not attempt it.** Such anchors are treated as *carrying no quantity
evidence*, so ordering and text place them instead. They must stay a small
minority (`SUBSEQ_MAX_WILDCARD_RATE`, 10%; 徐汇 has 7 of 170 = 4.1%) or the
quantity criterion becomes decorative.

### 4.3 Why this cannot be loosened further

Two guards, both measured rather than assumed:

- **Distinctiveness.** A list whose quantities are all `1` admits *any*
  order-preserving assignment. `_chance_agreement` (already used by the
  exact-count path) rejects such evidence.
- **Wildcard cap.** See above.

Per-row conflict isolation (unit / family / DN) still runs afterwards exactly as
for the exact-count path; a conflicting row still goes to `pending` alone rather
than poisoning the table.

## 5. The E2E now pins this at 100%, with no tolerance

`_ZERO_MODEL_FULL_ALIGNMENT = {"A3": (89, 89), "B3": (170, 136)}` — anchors and
per-supplier quote rows, **counted from the source documents, not read back from
the system.** Every supplier must reach `matched_rows == quote_rows` with
`pending == residue == validation_failed == 0`, and the run must report
`low_conf == 0` and `matched_quotes == total_quotes`.

No baseline, no tolerance. A deterministic path that does not align is a defect.

Verified live: setting B3's expected rows to 135 fails the test; 136 passes.

Two test-side habits were corrected along the way, both instances of the same
mistake:

- `_recognize_tender` **replicated** `/tender-list/preview`'s internals rather
  than calling it, on the stated grounds that they were 「完全同一套调用」. They
  were not — the route picks a sheet and the replica did not — so the multi-sheet
  defect was invisible to the suite *by construction*.
- The A3 refusal was recorded as a permanent `skip` with a prose explanation
  claiming the fix "needs a field + migration, out of scope". It needed neither;
  it needed three column patterns.

## 6. What this does not change

- The recognition path (PDF/Paddle). A1/B1/B2 are untouched; their baselines are
  unchanged.
- Semantic matching. It remains the fallback whenever the deterministic gates
  refuse, and the gates still refuse by default.
- The 34 unquoted 徐汇 anchors stay unquoted. `anchors_covered` is 136 of 170 and
  that is the honest number — a supplier that did not price an item has not
  priced it, and no amount of alignment machinery should invent one.

## 7. Open question

**Column mapping is still a keyword table, and it should probably not be.**
§2 was fixed by adding six regex patterns. The next file that writes 「除税单价」
or 「金额（不含税）」 will need six more. The mapping is a schema-interpretation
problem — exactly the kind an LLM generalizes well — and crucially it is
**deterministically verifiable**: does the quantity column parse as numbers, does
`qty × unit_price ≈ total` close, does `total × (1+rate) ≈ incl`? A model that
guesses wrong is caught by arithmetic before anything is stored, which is a very
different risk profile from letting a model decide *rows*.

Row alignment is the opposite case and should stay where §4 leaves it: the
quantity sequence solves it exactly, and CLAUDE.md's 「LLM 不得重排候选」 applies
squarely.

Not scoped here. Recorded so the next person does not read §2 as an endorsement
of growing the table.
