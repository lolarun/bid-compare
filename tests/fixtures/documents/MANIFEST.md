# Fixture manifest — design/28 corpus

> First-class deliverable, not a README (design/28 §6). This is the **labeled
> answer set** for the auto-classifier's acceptance test (cut 2): the
> classifier's Tier 0/1/2 output is diffed against the "True type" column
> below.
>
> **2026-08-23**: this note used to say `金桥地体上盖项目-采购清单.xlsx` was
> expected back as **不确定** and that this was a pass. It is now expected as
> **采购清单 (definitive)** — its price columns turned out to hold no price at
> all (one column blank, three columns entirely `0`); see its row. The
> `uncertain` branch is still covered, by a synthetic half-priced sheet in
> `test_document_classify`, because a judgement boundary is the right thing to
> guard with a synthetic sample — only business facts need real corpus.
>
> Scenario grouping (A/B/C) is recorded here, not in filenames — this is what
> lets the E2E suite (design/28 §7 cut 7) run each scenario independently.
> Source: real customer documents supplied 2026-08-13, see
> `docs/design/28-document-auto-classification.md` §1/§4.
>
> **2026-08-23**: the four `徐汇…报价清单.csv` files are now recorded here.
> They were on disk and in use (they are the stage-one answer keys) but absent
> from this table — and `test_manifest_corpus_full_coverage` globbed only
> `*.xlsx`/`*.xls`/`*.pdf`, so "full corpus coverage" was green while
> `classify_tier0` returned `None` for every one of them and the UI answered
> 「不支持的文件类型」. An answer set that omits a file type cannot catch a
> defect in that file type. Corpus is now **18** files.
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
| `金桥地体上盖项目-采购清单.xlsx` | 采购清单 (tender_list) | B | — (this *is* the list) | 94 rows, 1 sheet (`阀门`) | **Reclassified 2026-08-23** — was recorded here as "不确定 (ambiguous, by design)". The re-measurement that follows is what changed it: the 3 price columns contain **no price at all**. `单价（元）不含税` is entirely blank; `合计（元）不含税`, `税额（元）` and `价税合计（元）` are non-empty but every single value is literally `0`. The old fill rate of ~63% came from counting those zeros as "filled" — a per-cell rule that is right per cell (a written `0` is a different signal from a blank) and wrong per column (89 items priced at zero is a placeholder, not a quote). Two judgements were repaired: an all-zero price column no longer counts as a price column, and "price headers present but cells effectively empty" now has its own branch (`FILL_RATE_BLANK`) — previously only "almost full → bid_list" and "no price columns at all → tender_list" existed, so the most canonical procurement list shape had nowhere to land. Consequence of the old behaviour, observed in the running app: the modal's cancel key routed it to the bid path and **the procurement list became a supplier column in the comparison matrix** (0/89, total ¥0). The `uncertain` branch is still covered, by a synthetic half-priced sheet in `test_document_classify`. |
| `徐汇区华泾镇项目-采购清单.xlsx` | 采购清单 (tender_list) | C | — (this *is* the list) | 2 sheets: `矿物电缆` 95 rows, `普通电缆` 98 rows | **0 price columns detected** — the definitive signal for a procurement list (design/28 §2): a blank form with no prices is what bidders fill in. **Both sheets are list sheets** and are merged into one 170-item axis (`parse_tender_all_sheets`, design/39 §3); the sheet name becomes the item's `profession`, which is the *only* carrier of 矿物/普通 here since the columns are just 序号/名称/单位/数量. Earlier the preview parsed only the larger sheet, so the axis was 92 and the 44 mineral rows in every quote had nowhere to land. Sheet names corrected 2026-08-23: this row previously read «2 sheets (`招标清单`, `亨通报价`)», which matched no sheet in the file. Contains 7 composite rows whose parent quantity is blank/0 with the real figures on continuation rows (see design/39 §4.2). |
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
| `徐汇区华泾镇项目-上海浦东报价清单.csv` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Supplier's own answer key for the matching PDF; used as the stage-one accuracy reference (`SNAPSHOT_REFERENCE`). |
| `徐汇区华泾镇项目-亨通报价清单.csv` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same. 99% price-column fill (one row is a legitimate 不报价, printed `/` in the source). |
| `徐汇区华泾镇项目-宏胜报价清单.csv` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same. The clean reference of this set — recognition matches it 78/78 on unambiguously-keyed rows. |
| `徐汇区华泾镇项目-远东报价清单.csv` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same. |
| `徐汇区华泾镇项目-上海浦东报价清单.xlsx` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | xlsx conversion of the `.csv` of the same name, added 2026-08-23 during manual testing. Parses to the identical 136 items and the identical total (¥20,629,762.68), so it doubles as a container-independence check: same data, different container, same answer. |
| `徐汇区华泾镇项目-亨通报价清单.xlsx` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same; total ¥20,966,959.43. |
| `徐汇区华泾镇项目-宏胜报价清单.xlsx` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same; total ¥20,597,048.37. |
| `徐汇区华泾镇项目-远东报价清单.xlsx` | 报价清单 (bid_list) | C | — (this *is* the list) | 137 rows | Same; total ¥20,014,715.08. |

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
