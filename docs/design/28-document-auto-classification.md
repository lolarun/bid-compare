# 28 — Drop-anything Upload: Automatic Document Classification & Pairing

> **Status — CONFIRMED 2026-08-13, scheduled AFTER design/27 §10 step 5.**
> Trigger: user request — "希望用户能够直接上传招投标文件，而不是拖动到指定区域…
> 通过 LLM 智能分析自动在第二个界面（解析确认）中自动生成". The user supplied a
> 14-file real-document corpus as both the test set and the acceptance basis.
>
> Scheduling decision (user, 2026-08-13): this lands **after** design/27 step 5
> (wizard retirement + prj2 regression), not before — step 5 is the closing of
> the current refactor, and the new upload screen is built on top of the new
> workspace. Two rounds must not interleave.
>
> Relationship to other docs: consumes design/27's workspace shell (the upload
> screen replaces its materials strip's typed slots); consumes design/26's
> recognition output; extends design/24's "list slot is the primary source"
> decision (§5.2). Gate semantics from CLAUDE.md §4 unchanged throughout.

## 1. Goal

Today the user must know *which slot* each file belongs in (招标文件 / 采购清单 /
投标文件) and drag it there. Target: **drop everything at once, the system
works out what each file is**, and screen 2 shows the derived structure for
confirmation before anything is committed.

Input space (all four occur in the supplied corpus):

| Kind | With embedded list | Without |
|---|---|---|
| 招标文件 PDF | 金桥地体上盖招标文件.pdf | prj2 电缆招标.pdf |
| 投标文件 PDF | 绵存/凯硕/泰科龙/浦东/亨通/宏胜/远东 | (occurs in practice) |
| 采购清单 | 金桥…xlsx, prj2 附件一…xlsx | — |
| 报价清单 | 绵存/凯硕/泰科龙 投标清单.xlsx | — |

## 2. Feasibility: measured, not assumed

Measured 2026-08-13 against the real corpus, using the **existing**
`vl_quote.map_columns` price-slot mapper on each workbook's header row:

| File | Price columns detected | Fill rate | Verdict |
|---|---|---|---|
| 凯硕新正投标清单.xlsx | 3 | **60/60 = 100%** | quote list (strong) |
| prj2 附件一电缆清单.xlsx | **0** | — | procurement list (**definitive** — a list with no price column at all is the blank form bidders fill in) |
| 金桥地体上盖招标文件.xlsx | 3 | **32/60 ≈ 53%** | **genuinely ambiguous** |

The third row is the important one: it is *really* ambiguous — the user's own
manual grouping filed it under 投标清单 while its filename says 招标文件. A
classifier that reports "unsure" here is behaving **correctly**. This defines
the architecture: settle what is settleable deterministically, surface the rest.

**Conclusion: feasible, and cheaper than an LLM-first design** — most signals
are already produced by the existing pipeline at zero marginal cost.

## 3. Three-tier signal ladder

```
Tier 0 — instant (<1s, no model)
  · extension: xlsx/xls → list-class; pdf → document-class
  · Excel: run map_columns on the header row
        no price columns          → 采购清单 (definitive)
        price columns ~100% filled → 报价清单 (strong)
        partially filled / unclear → defer to Tier 2
  · PDF: has_usable_text_layer (design/25) → native vs scanned
              ↓
Tier 1 — post-recognition (recognition runs anyway; zero extra cost)
  · cover scalars (paddle_doc_meta): 招标编号/招标人 → tender;
    投标单位/投标人 + supplier_name present → bid
  · table artifact: priced table found → bid; unpriced list → tender
  · row_count / has_price_column from the draft
              ↓
Tier 2 — LLM, residual only
  · input: cover text + header row + 3 sample rows (plain-text call via the
    existing text_call channel — no vision call)
  · answers exactly one multiple-choice question (招标 / 投标 / 不确定);
    never reorders, never rewrites values
  · "不确定" is a valid answer and must be passed through, never guessed
```

**Tier 2 is deliberately built last** (§7): only after Tiers 0/1 run against
the full corpus do we know the residual rate. If the residue is one or two
files, a user click is cheaper than a model call and Tier 2 may not be built
at all. Do not add the LLM branch before the residue is measured.

## 4. The corpus is three E2E scenarios, not one mixed pile

**User clarification (2026-08-13): the PDF set and the Excel set are two
separate end-to-end test cases, not paired inputs to one comparison.** This
materially simplifies the design — an earlier draft treated PDF+Excel
coexistence as the core problem (pairing, primary-source arbitration); it is
in fact an edge case, not the main path.

| # | Scenario | Tender side | Bid side | What it exercises |
|---|---|---|---|---|
| **A** | 金桥 all-PDF | 金桥招标.pdf (embedded list) | 绵存 / 凯硕 / 泰科龙 .pdf | Full PDF path: scanned recognition, Paddle, quality gates |
| **B** | 金桥 all-Excel | 金桥招标.xlsx | 绵存 / 凯硕 / 泰科龙 投标清单.xlsx | Full deterministic path: zero model calls end-to-end |
| **C** | prj2/prj1 mixed-by-necessity | prj2 招标.pdf (**no** embedded list) + prj2 附件一清单.xlsx | prj1 上海浦东 / 亨通 / 宏胜 / 远东 .pdf | The case that drove design/24: tender PDF carries no list, so an Excel supplement is *required*, bids remain PDFs |

Scenario B is the more valuable of the first two for regression purposes: it
runs the whole comparison with **no recognition model in the loop**, so any
failure it produces is unambiguously in the business layer, not the engine.
Scenario C is the acceptance case for the design/24-27 workspace.

### 4.1 Same-supplier duplication — edge case, one rule, no arbitration UI

If a supplier does end up with both a PDF and an Excel in one session (allowed,
just not the designed path): **the Excel is primary, the PDF becomes
cross-check**. Rationale: `tabular_ingestion` parses spreadsheets
deterministically with no OCR error surface, while the PDF path carries the
measured field-accuracy ceiling (design/26 §6). This extends design/24's
decision 6 ("whatever is in the list slot is primary") consistently, and turns
the redundant file into free reconciliation evidence — `source_reconcile`
already implements that check; reuse it, do not write a second one.

No dedicated arbitration UI is built for this. The confirmation screen shows
the derived role of every file (§5 red line 1) and the user can re-assign; that
is sufficient for an edge case.

### 4.2 Supplier attribution (replaces the former "pairing problem")

What remains necessary is attributing each **bid** file to a supplier — a
single-file question, not a cross-file matching one:
- Primary signal: `supplier_name` from cover extraction (restored in
  design/27 §7 item 1).
- Filename: **hint only, never decisive** (§5 red line 2).
- Unresolved attribution surfaces on the confirmation screen as an editable
  field, never guessed silently.

## 5. Red lines

1. **Classification is shown, editable, and may say "不确定"** — screen 2 is a
   confirmation screen, not a display screen (design/27 red line 2).
2. **Filename is a hint, never a decision** — `.claude/rules/recognition.md`
   forbids deciding roles by supplier name, fixed page numbers, or sample
   filenames. A classifier that would flip its answer if the file were renamed
   `1.pdf` has failed this line.
3. **Low confidence is labeled, never guessed** — same precedent as the
   orientation pre-check ("无过半共识则不转并标 REVIEW").
4. **No silent commitment** — nothing is written to the project until the user
   confirms screen 2.

## 6. Test fixture reorganization (14 files)

The user selected exactly these 14 (count corrected from an earlier "12");
unrelated material (询标疑问, 合同/投标书样式, `_cmp.py`, intermediate
`.csv`/`.doc`) stays under `docs/` and is **not** moved.

```
tests/fixtures/documents/
  tender/            金桥地体上盖招标文件.pdf          (with embedded list)
                     prj2_电缆招标.pdf                 (no embedded list)
  tender_list/       金桥地体上盖招标文件.xlsx         (ambiguous — see §2)
                     prj2_附件一_电缆清单.xlsx
  bid/               上海绵存投标文件.pdf
                     凯硕新正投标文件.pdf
                     泰科龙投标文件.pdf
                     prj1_上海浦东.pdf / 亨通.pdf / 宏胜.pdf / 远东.pdf
  bid_list/          上海绵存投标清单.xlsx
                     凯硕新正投标清单.xlsx
                     泰科龙投标清单.xlsx
  MANIFEST.md
```

- **These files are already git-tracked** (35 files under `docs/test*`), so the
  move is a plain `git mv` — history is preserved and the repo gains nothing
  in size. The earlier idea of gitignoring them plus a SHA-based fetch script
  is dropped as unnecessary complexity.
- **`MANIFEST.md` is a first-class deliverable, not a README**: for each file —
  true type, **which E2E scenario (A/B/C, §4) it belongs to**, whether it
  embeds a list, row count, and known quirks (transposed table, duplicate
  copies, no 序号 column, decimal quantities). It doubles as the **labeled
  answer set** for classifier acceptance: the classifier's output is diffed
  against MANIFEST, and 金桥…xlsx is expected to come back "不确定" — that is
  a pass, not a failure.
- The scenario grouping in MANIFEST is what lets the E2E suite run each
  scenario independently; do not encode scenario membership in filenames.
- **55 code references to `docs/test*`** across `apps/api/tests` and `scripts/`
  must be updated in the same commit; full-suite green is the acceptance.

## 7. Delivery plan (3-4 days, after design/27 step 5)

| Cut | Content | Est. |
|---|---|---|
| 1 | Fixture move (`git mv` ×14) + MANIFEST with true labels + update 55 references | 0.5d |
| 2 | Tier 0 classifier (Excel header/fill via `map_columns`, PDF text-layer probe) + unit tests over the whole MANIFEST corpus | 0.5d |
| 3 | Tier 1 signal fusion (cover scalars, table artifacts) + confidence model | 1d |
| 4 | Supplier attribution + Excel-primary edge rule + `source_reconcile` reuse (shrunk from 1d after the §4 clarification — no pairing engine, no arbitration UI) | 0.5d |
| 5 | Upload screen (drop N files) + confirmation screen (editable classification, ambiguity marked) | 1d |
| 6 | Tier 2 LLM residue handler — **only after cut 3 reports the real residue rate**; may be dropped | 0.5d |
| 7 | Wire scenarios A/B/C as runnable E2E cases against the new upload flow | 0.5d |

Scenario B (all-Excel, zero model calls) should be the **first** scenario wired
in cut 7: it is fast, free, deterministic, and isolates business-layer defects
from engine defects.

## 8. Out of scope

- Invite (邀标) flow reuse of the same uploader — later.
- Any change to recognition accuracy, gate semantics, or matrix logic.
- Editing of the procurement list (design/27 D2's conditional clause governs).

## 9. Open items

- Residual rate after Tiers 0/1 — unknown until cut 3; determines whether
  cut 6 is built.
- Whether a bid PDF *without* an embedded quote list occurs in the corpus
  (the table above marks it "occurs in practice" but no sample is present) —
  if none, that branch stays untested until a real one appears; record it as
  an untested path rather than claiming coverage.
