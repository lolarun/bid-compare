# 30 — Procurement-list / quote-list generalization

> **Status: proposal, nothing implemented.** Everything below §2 is
> measurement against real corpus; §3 onward is a proposed approach that has
> not been agreed or built. Do not read the numbers in §2 as "handled".

## 1. Trigger

Round-3 UI feedback (design/29 §9) ended with: *"完成之后查一下采购清单和
报价清单的泛化能力，因为采购清单格式不一样，要想一个办法."* Two projects in
the corpus have deliberately different list shapes:

- `金桥地体上盖项目` — 阀门, one sheet, 两级表头 (材质 spanning 阀体/阀芯/
  阀板/阀杆/密封圈), quotes as `.xlsx`.
- `徐汇区华泾镇项目` — 电缆, **two** sheets (矿物电缆 / 普通电缆), no 规格
  column at all, quotes as `.csv`.

## 2. Measured — what actually happens today

Run against the real fixtures through the production entry points
(`tender_list.parse_tender_xlsx`, `tabular_ingestion.extract_quote_tabular`,
`paddle_vl.recognize_quote_paddle` via snapshot replay). No API cost.

### 2.1 金桥 (阀门) — the shape the system was built for

| | items | name | spec |
|---|---|---|---|
| 采购清单 `.xlsx` | 89 | `Y型过滤器` | `DN20` |
| 报价清单 `.xlsx` (×3) | 89 | `Y型过滤器` | `DN20` |

Column roles line up; alignment has a real key. **One defect:** the xlsx
anchor parser returns `materials = {}` for every row — the 材质 two-level
sub-columns are dropped. The PDF path (`vl_tender.build_tender_fields`)
*does* collect them into `materials`. Same tender, two sources, different
anchor content.

### 2.2 徐汇 (电缆) — the shape that breaks

| | items | name | spec |
|---|---|---|---|
| 采购清单 `.xlsx` 矿物电缆 sheet | 78 | `RTTYZ-3*240+2*120` | *(empty)* |
| 采购清单 `.xlsx` 普通电缆 sheet | 92 | `YFD-WDZA-YJY-3*240+2*120` | *(empty)* |
| 报价清单 `.csv` (×4) | 136 | `矿物电缆` | `RTTYZ-3*240+2*120` |

Three independent problems, in order of severity:

**G1 — multi-sheet procurement lists are silently truncated.**
`pick_default_sheet` picks the sheet with the most data rows. Both sheets
here `looks_like_list=True`; it returns 普通电缆 (92) and **78 items from
矿物电缆 are dropped with no warning anywhere**. The quotes cover both
(92 普通电缆 + 44 矿物电缆 = 136), so today the comparison can only ever see
part of the tender. There is a sheet switcher in the UI (design/24 B1), but
nothing tells the user that the other sheet exists or that the count is
partial — the card would read "采购清单 92 项" for a 170-项 file. This is a
correctness defect, not a matching-quality issue.

**G2 — the name/spec column roles are offset between the two documents.**
The procurement list puts the cable model in 名称 and has no 规格 column;
the quote puts a category word in 材料/设备名称 and the model in 规格型号.
So `anchor.name` corresponds to `quote.spec`, and `anchor.spec` is empty.
Any mapping that assumes "name matches name, spec matches spec" has nothing
to work with here. This is the concrete form of "采购清单格式不一样".

**G3 — model text varies in ways that are not semantic.**
Matching 采购清单 `name` against 报价清单 `spec` as literal strings:

| key | matched | of quote's unique models |
|---|---|---|
| raw text | 110 | 134 |
| after NFKC + upper + `×`/`x`/`X`/`*` → `*` + width/dash fold | **125** | 127 |

Two unmatched remainders survive normalization and are genuinely different,
not noise: `WDZA-YJY-4*75+E35` (a size the tender does not list) and
`预分支电缆头YFD-WDZA-YJY-4*70+E35` (a composite assembly item the bidders
added). Those belong in the pending/REVIEW lane, not in a normalizer.

### 2.3 Quote-side field coverage — two column tables, different reach

`vl_quote._SLOTS` (recognizer) and `tabular_ingestion._TABULAR_COLUMN_PATTERNS`
(Excel/CSV) are separate mappings. The tabular one has no `tax_rate`,
`tax_amount`, or `total_price_excl_tax` role at all:

| source | 凯硕 `.xlsx` | 泰科龙 `.xlsx` | 凯硕 PDF (replay) |
|---|---|---|---|
| `unit_price` | 71.0 | **None** | None *(correct — see design/29 §11.1)* |
| `total_price` | 71.0 | 78.1 | None *(correct)* |
| `unit_price_excl_tax` | 62.83 | 69.12 | 62.83 |
| `total_price_excl_tax` | **None** | **None** | 62.83 |
| `tax_rate` / `tax_amount` | **None** | **None** | 0.13 / 8.17 |
| `price_basis` / `effective_*` | **None** | **None** | dual / 71.0 |

Same supplier, PDF vs Excel, different field set — and the tabular path
never runs the price-basis bridge, so `price_basis` is only derived later at
confirm time (`quote_confirmation_service` line ~676). 泰科龙's `.xlsx`
losing `unit_price` entirely is the sharpest case: `单价(不含税)` is claimed
by the excl slot and the file has no second 单价 column, so the generic slot
stays empty.

## 3. Proposed approach

Ordered by (defect severity × cost). Nothing here is built.

**P1 — stop the silent sheet truncation (G1).** Parse *every*
`looks_like_list` sheet and report the per-sheet counts. Either import all
sheets (anchors carry the sheet name, which is already the category word —
矿物电缆 / 普通电缆) or make the user choose explicitly; what must not
survive is picking one and reporting its count as the list's count. This is
the only item here that changes a number the user currently sees.

**P2 — one column-mapping table, not two (§2.3).** Fold the tabular
patterns into `vl_quote._SLOTS`, which already handles tax basis with the
A2 guards, and run the same `derive_price_basis` on the tabular path. Excel
and PDF for the same supplier then produce the same field set. Mechanical,
well covered by existing tests, no new concepts.

**P3 — align on a normalized identity, not on one column (G2 + G3).**
Two parts, both deterministic:

- *Identity text.* Build the matching key from the mapped
  name + spec + model fields **joined**, not from a single column, so a list
  that puts the model in 名称 and one that puts it in 规格型号 produce the
  same key. This is the same conclusion HANDOFF §5 item 7 reached from the
  other direction ("规格文本应降级为校验而非对齐主键") — spec becomes
  evidence, identity becomes the axis.
- *Normalization.* NFKC, upper-case, `×`/`x`/`X`/`*` → one separator,
  width/dash folding. Measured lift on this corpus: 110 → 125 of 127.
  The original text is kept untouched; normalization applies to the
  matching key only (CLAUDE.md: auto-correction keeps the original value,
  its basis, and a flag).

**P4 — 材质 sub-columns in the xlsx anchor parser (§2.1).** Reuse the
父列_子列 → `materials` logic the PDF path already has, so the two sources
of the same tender stop disagreeing.

## 4. Explicitly not proposed

- No per-project or per-file special-casing. Every rule above keys off
  structural facts (how many list sheets, which column roles mapped, what
  the text normalizes to), never off a project name, supplier name, sheet
  name, or fixed column order (CLAUDE.md §1).
- No fuzzy/embedding matching in P3. The measured residue after
  normalization is 2 items, both genuinely absent from the tender — those
  belong in the pending lane where a human decides, not in a similarity
  threshold.
- No change to gate semantics, evaluation totals, or the bid-matrix
  computation.

## 5. Open question for the user

P1 changes what "采购清单 N 项" means for a multi-sheet file (92 → 170 for
徐汇). That is the honest number, but it is a visible change to an existing
project's displayed count, and it implies anchors from two categories share
one comparison. Confirm before building.
