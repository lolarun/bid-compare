# Fixture manifest — design/28 corpus

> First-class deliverable, not a README (design/28 §6). This is the **labeled
> answer set** for the auto-classifier's acceptance test (cut 2): the
> classifier's Tier 0/1/2 output is diffed against the "True type" column
> below. `金桥地体上盖项目-采购清单.xlsx` is expected to come back **不确定** —
> see its row — that is a **pass**, not a failure (design/28 §2).
>
> Scenario grouping (A/B/C) is recorded here, not in filenames — this is what
> lets the E2E suite (design/28 §7 cut 7) run each scenario independently.
> Source: real customer documents supplied 2026-08-13, see
> `docs/design/28-document-auto-classification.md` §1/§4.
>
> **2026-08-21**: files renamed flat (`{project}-{doc}.ext`, no subdirectory)
> and moved directly under `documents/`; the `tender/`/`bid/`/`tender_list/`/
> `bid_list/` prefixes in this table below are historical and no longer exist
> on disk. Project name replaces the old `prj1`/`prj2` codes: `金桥地体上盖项目`
> = old 金桥 set, `徐汇区华泾镇项目` = old prj1/prj2 set.

## Files

| File | True type | Scenario | Embeds list? | Rows/pages | Known quirks |
|---|---|---|---|---|---|
| `金桥地体上盖项目-招标文件.pdf` | 招标文件 (tender) | A | Yes | 18 pages | Scanned; recognition goes through the full OCR/Paddle path. |
| `徐汇区华泾镇项目-招标文件.pdf` | 招标文件 (tender) | C | **No** — this is *why* Scenario C needs an Excel supplement at all (design/24's original driver, design/28 §4 row C). | 11 pages | — |
| `金桥地体上盖项目-采购清单.xlsx` | **不确定 (ambiguous, by design)** | B | — (this *is* the list) | 94 rows, 1 sheet | design/28 §2 cited 32/60 ≈ 53% fill; re-measured during cut 2 implementation (`document_classify.classify_excel`) at **62.7%** — the 3 price columns are a mixed signal (`单价（不含税）` entirely blank, `合计（不含税）`/`含税合计` entirely literal `"0"`, which is filled-but-placeholder, not empty). Different measurement, same qualitative conclusion: well below the 90% "strong" threshold, correctly returns 不确定. The user's own manual grouping filed it under 投标清单 despite the tender-side filename. Tier 0/1 classifiers must return 不确定 here, not guess. |
| `徐汇区华泾镇项目-采购清单.xlsx` | 采购清单 (tender_list) | C | — (this *is* the list) | 95 rows, 2 sheets (`招标清单`, `亨通报价`) | **0 price columns detected** — the definitive signal for a procurement list (design/28 §2): a blank form with no prices is what bidders fill in. |
| `金桥地体上盖项目-上海绵存投标文件.pdf` | 投标文件 (bid) | A | Yes | 31 pages | Exceeds `MAX_PAGES=30` (project memory: `project_e2e_fixtures`). Row extraction gap is in the extraction layer, not OCR (project memory: `project_rootcause_layers`) — OCR itself is complete. Golden `name` column uses the Excel's own naming, not the PDF's literal item name — do not use raw PDF name as an accuracy baseline (project memory: `project_miancun_golden_naming`). |
| `金桥地体上盖项目-凯硕新正投标文件.pdf` | 投标文件 (bid) | A | Yes | 19 pages | — |
| `金桥地体上盖项目-泰科龙投标文件.pdf` | 投标文件 (bid) | A | Yes | 53 pages | Exceeds `MAX_PAGES=30`. Root cause of prior recognition failures on this file was **whole-page 90° rotation**, not a transposed table or an engine defect — pre-rotating to upright before recognition made the golden set match fully (project memory: `project_rootcause_layers`). |
| `徐汇区华泾镇项目-上海浦东投标文件.pdf` | 投标文件 (bid) | C | Yes | 15 pages | Real regression (2026-08-14): 263 rows recognized; confirms with `checksum_ack` (declared-vs-line-sum deviation 0.608%, just over the 0.5% threshold — a genuine small discrepancy, not an extraction bug). |
| `徐汇区华泾镇项目-亨通投标文件.pdf` | 投标文件 (bid) | C | Yes | 11 pages | Real regression (2026-08-14): 132 rows recognized; 6 rows genuinely have no unit price in the source document (not an extraction gap) — correctly blocks at `missing_total_requires_review` and must not be force-filled. |
| `徐汇区华泾镇项目-宏胜投标文件.pdf` | 投标文件 (bid) | C | Yes | 11 pages | Real regression (2026-08-14): 132 rows recognized, confirms cleanly with zero gate issues — the "clean" reference document in this set. |
| `徐汇区华泾镇项目-远东投标文件.pdf` | 投标文件 (bid) | C | Yes | 19 pages | Real regression (2026-08-14): 139 rows recognized; 13 rows genuinely have no unit price in the source document — same `missing_total_requires_review` situation as 亨通, correctly blocks. |
| `金桥地体上盖项目-上海绵存报价清单.xlsx` | 报价清单 (bid_list) | B | — (this *is* the list) | 91 rows, 1 sheet | — |
| `金桥地体上盖项目-凯硕新正报价清单.xlsx` | 报价清单 (bid_list) | B | — (this *is* the list) | 91 rows, 1 sheet | **60/60 = 100%** price-column fill via `map_columns` — the strong/unambiguous reference case for this type (design/28 §2). |
| `金桥地体上盖项目-泰科龙报价清单.xlsx` | 报价清单 (bid_list) | B | — (this *is* the list) | 91 rows, 1 sheet | — |

## Scenarios (design/28 §4)

| # | Scenario | Tender side | Bid side | Exercises |
|---|---|---|---|---|
| **A** | 金桥 all-PDF | `金桥地体上盖项目-招标文件.pdf` | `金桥地体上盖项目-上海绵存投标文件.pdf`, `金桥地体上盖项目-凯硕新正投标文件.pdf`, `金桥地体上盖项目-泰科龙投标文件.pdf` | Full PDF path: scanned recognition, Paddle, quality gates. |
| **B** | 金桥 all-Excel | `金桥地体上盖项目-采购清单.xlsx` | `金桥地体上盖项目-上海绵存报价清单.xlsx`, `金桥地体上盖项目-凯硕新正报价清单.xlsx`, `金桥地体上盖项目-泰科龙报价清单.xlsx` | Full deterministic path — **zero model calls end-to-end**; any failure here is unambiguously in the business layer, not the engine. First scenario to wire in cut 7. |
| **C** | 徐汇区华泾镇项目 mixed-by-necessity | `徐汇区华泾镇项目-招标文件.pdf` (no embedded list) + `徐汇区华泾镇项目-采购清单.xlsx` | `徐汇区华泾镇项目-上海浦东投标文件.pdf`, `徐汇区华泾镇项目-亨通投标文件.pdf`, `徐汇区华泾镇项目-宏胜投标文件.pdf`, `徐汇区华泾镇项目-远东投标文件.pdf` | The case that drove design/24: tender PDF carries no list, so the Excel supplement is *required*; bids remain PDFs. Acceptance case for the design/24-27 workspace — real end-to-end regression run against this exact set 2026-08-14 (see design/27 §10 step 5 commit). |

## Not moved

Per design/28 §6, unrelated material stays under `docs/` — not part of this
corpus: 询标疑问 (query documents), 合同/投标书样式 (contract/bid-format
templates), `docs/test1/prj1/_cmp.py`, intermediate `.csv`/`.doc` files,
`docs/test/BID_E2E_TEST_PLAN.md`, `docs/test/E2E_FIXTURES.md`,
`docs/test/徐汇区华泾镇D5B一期桥架...` (a different material category, not
part of the cable scenario), `docs/test/材料采购招标文件审批表.pdf` (an
approval form, not a recognition fixture), and the `.zip`/`.7z` archives
under `docs/test1/`.
