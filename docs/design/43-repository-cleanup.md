# 43 — Repository cleanup

> **Status: plan, approved 2026-08-26, not yet executed.** §1 is measured, not
> estimated (`git ls-files` + blob-hash dedup over the whole tree). §2 records
> the three decisions the user made on the forks; those forks are closed and
> should not be reopened without new information.

## 1. What is actually in the repository

294.1 MB tracked, **275.9 MB (93.8%) of it under `docs/`**. Code is 11.3 MB.

| Directory | Size | Files |
|---|---|---|
| `docs/项目资料/` | 119.00 MB | 50 |
| `docs/test1/` | 96.22 MB | 17 |
| `docs/data/` | 38.03 MB | 215 |
| `docs/` (root) | 13.67 MB | 19 |
| `apps/api/` | 9.69 MB | 279 |
| everything else | 17.5 MB | ~230 |

`.git` is 379 MB.

## 2. Decisions taken (2026-08-26)

1. **Working-tree cleanup only — no history rewrite.** Deleting files does not
   shrink `.git`; only `filter-repo` would, at the cost of invalidating every
   existing clone and every commit SHA while a GitHub Actions deploy is live.
   Not worth it. **Consequence to state honestly: none of the deletions below
   reduce clone size.** They reduce confusion, not bytes.
2. **Duplicate corpus: delete the `docs/` copy, keep `tests/fixtures/`.**
3. **One-off scripts: move to `scripts/archive/`, do not delete.**

## 3. Phase 0 — the repository is currently broken for a clean clone

**This is not cleanup. It is a defect, and it blocks everything else.**

The fixture rename to flat `{project}-{document}.ext` was *documented and
committed* — `tests/fixtures/documents/MANIFEST.md` describes the flat layout
(dated 2026-08-21) and four committed test modules already import the new
names:

```
apps/api/tests/test_invite_integration.py:656
apps/api/tests/test_tender_pdf_extract.py:29-30
apps/api/tests/test_tender_text_layer.py:25-26
apps/api/tests/test_vl_tender.py:172
```

**The files themselves were never committed.** Git still holds the old
four-directory layout (`bid/`, `bid_list/`, `tender/`, `tender_list/`, with
`prj1_`/`prj2_` prefixes). A clean clone of `main` cannot run those tests. It
passes locally only because the renamed files sit untracked in the working
tree. The likely cause is that the large binaries were skipped during the
original `git add`.

All 14 old files map to a new file **by content hash, exactly** — nothing was
edited, only renamed:

```
prj1_上海浦东.pdf          → 徐汇区华泾镇项目-上海浦东投标文件.pdf
prj1_亨通.pdf              → 徐汇区华泾镇项目-亨通投标文件.pdf
prj1_宏胜.pdf              → 徐汇区华泾镇项目-宏胜投标文件.pdf
prj1_远东.pdf              → 徐汇区华泾镇项目-远东投标文件.pdf
prj2_电缆招标.pdf          → 徐汇区华泾镇项目-招标文件.pdf
prj2_附件一_电缆清单.xlsx   → 徐汇区华泾镇项目-采购清单.xlsx
上海绵存投标文件.pdf        → 金桥地体上盖项目-上海绵存投标文件.pdf
凯硕新正投标文件.pdf        → 金桥地体上盖项目-凯硕新正投标文件.pdf
泰科龙投标文件.pdf          → 金桥地体上盖项目-泰科龙投标文件.pdf
上海绵存投标清单.xlsx       → 金桥地体上盖项目-上海绵存报价清单.xlsx
凯硕新正投标清单.xlsx       → 金桥地体上盖项目-凯硕新正报价清单.xlsx
泰科龙投标清单.xlsx         → 金桥地体上盖项目-泰科龙报价清单.xlsx
金桥地体上盖招标文件.pdf    → 金桥地体上盖项目-招标文件.pdf
金桥地体上盖招标文件.xlsx   → 金桥地体上盖项目-采购清单.xlsx
```

Plus 8 genuinely new files (`徐汇区华泾镇项目-{上海浦东,亨通,宏胜,远东}报价清单.{csv,xlsx}`)
— the stage-one answer keys, already described in MANIFEST.

**The rename itself is correct and must not be reverted**: `prj1_`/`prj2_` are
exactly the sample-specific codes CLAUDE.md forbids, and the new names carry
the project identity the scenario table needs.

### 3.1 What still points at the old layout

Committing the moves without fixing these turns a silent breakage into a loud
one, which is an improvement, but both should land together:

| File | Reference |
|---|---|
| `apps/api/tests/test_cable_accuracy_e2e.py:55` | `.../documents/bid").glob(...)` — `next()` with no default, raises rather than skips |
| `apps/api/tests/test_tender_text_layer.py:57` | `.../documents/tender/不存在.pdf` — a negative-path assertion, but the parent directory is going away |
| `apps/api/tests/test_paddle_quote_api_e2e.py:22` | docstring cites `tests/fixtures/documents/bid` |
| `scripts/api_e2e_compare.py:17-19` | usage example |
| `scripts/stitch_vs_multi_bench.py:52-56` | hard-coded paths, three documents |
| `scripts/tender_vl_probe.py:16-17` | hard-coded paths |
| `scripts/test_tender_pdf.py:6-7,19-21` | defaults |
| `scripts/test_e2e_alignment.py:3`, `test_e2e_experiment_a.py:9` | docstrings |
| `scripts/test_ocr_render_ab.py:9,97` | default argument |
| `scripts/try_paddleocr_vl.py:66-72` | `PRJ1 / "prj1_*.pdf"` |
| `scripts/build_ocr_fixture.py:8`, `compare_models.py:4`, `test_glm.py:4` | usage examples |

### 3.2 Also uncommitted, and not cleanup either

Finished work sitting in the working tree, to be committed with or before
Phase 0 — listed so it is not mistaken for clutter:

- `docs/design/42-multi-round-quoting.md` + the whole multi-round feature
  (`models/quote_round.py`, `routes/quote_rounds.py`, `schemas/quote_round.py`,
  `services/tender/quote_round_service.py`, migrations `0009`/`0010`, four test
  modules)
- the ¥0 detail-total fix (`tabular_ingestion.py`) and the manual
  re-recognition escape hatch (`document_ingestion.py`, `routes/intake.py`,
  frontend, `test_re_recognize.py`)
- 6 new + 2 updated Paddle snapshots

The three `.docx` contracts and the 92 MB corpus are explicitly **out of scope
for this round** — the user reserved them for a separate pass.

## 4. Phase 1 — deletions with evidence

Nothing here needs a judgement call; each has a stated reason.

| Delete | Why |
|---|---|
| `tests/fixtures/ocr_snapshots/` (8 files) | **Zero references anywhere in the tree.** The legacy per-page OCR→HTML→TableGrid chain was physically deleted 2026-08-11 (`.claude/rules/recognition.md`). `vl_snapshots/` and `paddle_snapshots/` are both still live — only this generation is dead. |
| `docs/项目资料/初始资料/{泰科龙,上海绵存,凯硕新正}投标文件.pdf` (67.57 MB) | Byte-identical blobs to `tests/fixtures/documents/`. `docs/test/E2E_FIXTURES.md` already declares this copy 「仅作来源备份，不用于测试」. Decision §2.2. **Record in `E2E_FIXTURES.md` that `tests/fixtures/` is now the sole copy** — otherwise the next reader sees a backup that vanished. |
| `docs/test/材料采购招标文件审批表.pdf` | Byte-identical to the `docs/项目资料/初始资料/` copy; MANIFEST §"Not moved" already says it is not a recognition fixture. Keep the `项目资料` original. |
| `data/golden/{quote_miancun.json.bak, quote_miancun_pre_ocr_candidate.json.bak}` | `.gitignore`'s own stated reason for blanket-ignoring `data/*` is that `*.bak` backups hold real customer data; these two slipped in through the `data/golden/` allow-list. |
| `apps/www/src/components/PageHeader.vue`, `layouts/components/PageContainer.vue`, `layouts/components/WrapContent.vue`, `views/dashboard/components/PriceTrend.vue` | No import anywhere in `apps/www/src`. |
| `apps/www/package.json` → `"web": "file:"` | The package depends on itself. |
| `docs/test1/prj1/_cmp.py` | One-off comparison script with a hard-coded path; MANIFEST §"Not moved" already excludes it from the corpus. |

## 5. Phase 2 — `scripts/archive/`

`.gitignore` states the reason `scripts/` is tracked: 「诊断脚本承载了识别链路
的实验方法…不入库，下一个人只能重做一遍已经做过的实验」. That reason holds for
A/B rigs and scoring harnesses. It does not hold for a script that repaired one
specific submission in one specific database.

**Rule: `scripts/` root keeps what is re-runnable on new input. `scripts/archive/`
takes what is bound to a past incident, a retired chain, or a retired model.**

Archive (~55 of 94), by group:

- **Bound to specific records** — `audit_sub1819.py`, `retarget_sub1819.py`,
  `repair_proj63_sub1719.py`, `dump_sub18_bqls.py`, `extract_ocr_html.py`,
  `crop_missing_items.py`, `crop_p11_p12.py`, `audit_gate_misfire.py`,
  `_dump_gate_cells.py`, `verify_supplier_name_fix.py`,
  `check_kaishuoxinzheng.py` (also hard-codes a supplier name),
  `find_pdf_path.py`, `inspect_job_result.py`
- **The twelve `_*.py` one-shot data audits** — `_analyze_hist`, `_audit_data`,
  `_audit_materials`, `_brand_coverage`, `_check_blank_unit`, `_check_hvac_brand`,
  `_check_supplier_fk`, `_inspect_data`, `_preview_pdb`, `_verify_baselines`,
  `_verify_brands`, `_verify_quality`
- **Retired chains / retired models** — `test_two_stage.py` and
  `test_ocr_pipeline.py` (the deleted per-page chain), `bench_llm_models.py`,
  `compare_models.py`, `compare_glm_vs_sf32b.py`, `detail_compare.py`,
  `test_glm.py`, `test_qwen_ocr.py`
- **Old API smoke scripts** — `test_alignment_db_mode`, `test_bid_alignment`,
  `test_bid_alignment_apply`, `test_bid_matrix_alignment`, `test_bid_status`,
  `test_bid_status2`, `test_bid_status_dashboard`, `test_brand_recommend`,
  `test_excel_import`, `test_naming_rules`, `test_postprocess`,
  `test_single_supplier`, `test_tender_ext_attrs`, `test_e2e_anchor`,
  `test_e2e_enhance`, `test_e2e_alignment`, `test_e2e_tabular`,
  `test_e2e_experiment_a`, `test_e2e_llm_fill`, `test_e2e_llm_fill_batch`,
  `add_bid_status.py`, `create_test_excel.py`
- **Completed one-off migrations** — `migrate_v1_to_v2.py`,
  `convert_contract_md_to_docx.py`

Keep in `scripts/` root: the A/B rigs and scoring harnesses (`ab_uncertainty`,
`e2e_diff`, `audit_golden`, `score_paddleocr_vl`, `model_bakeoff_compare`,
`stitch_vs_multi_bench`, `vl_direct_bakeoff`, `vl_prod_e2e`,
`visual_stability_test`, `try_paddleocr_vl`, `try_page_classify_gate`,
`tender_vl_probe`, `probe_ocr_render_ab`), the fixture/golden builders
(`build_ocr_fixture`, `record_vl_snapshots`, `create_golden_fixtures`,
`rebuild_golden_from_excel`, `build_cable_golden`, `build_raw_assets`), the
data importers (`import_historical`, `import_brands`, `import_data`,
`convert_excel_to_csv`, `analyze_data`, `export_excel`, `seed_supplier_aliases`),
the measurement/reporting tools (`block_align_report`, `measure_anchor_alignment`,
`api_e2e_compare`, `p2_acceptance_run`, `export_customer_bid_matrix`,
`dump_tender`, `verify_rowcounts`, `probe_tender_pdf`), plus
`wipe_test_projects.py` and `deploy.sh`.

**Naming rule that falls out of this: no `test_*.py` in `scripts/` root.**
Two files survived the archive pass under the old name and were renamed
during execution — `test_ocr_render_ab.py` → `probe_ocr_render_ab.py`
(the plan's original call) and `test_tender_pdf.py` → `probe_tender_pdf.py`
(missed in the original enumeration above — it takes `pdf_path`/`xlsx_path`
arguments and is re-runnable on new input, the same shape as
`tender_vl_probe.py`, so it belongs in the keep list, not the archive).
Files named `test_*` outside `apps/api/tests/` and `tests/` are never
collected by pytest (`testpaths`), so the name promises a guarantee nothing
enforces.

`scripts/archive/README.md` records why each group was archived, mirroring
`docs/design/archive/README.md`.

## 6. Phase 3 — documentation hygiene

### 6.1 Nineteen dead cross-references

`docs/design/*.md` cite files that no longer exist. Each needs either a
retarget or an explicit "retired" note — a design doc pointing at a deleted
module is how a reader concludes the module still exists:

```
apps/api/intelligence/prompts.py               apps/api/intelligence/table_recognizer.py
apps/api/services/rebuild_submission_lines.py  apps/www/src/views/compare/IndexView.vue
docs/design/03-数据分析计划.md                  docs/design/06-功能设计.md
docs/ops/production-cleanup-runbook.md         docs/prompts.md
docs/technical-design.md                       docs/test/金桥地体上盖招标文件.pdf
scripts/{apply_cleanup_to_copy,audit_suppliers,build_production_fix_package,
         export_production_snapshot,merge_suppliers,recalculate_ref_prices,
         repair_project63,verify_db_state}.py
```

The pre-rename Chinese filenames (`03-数据分析计划.md`, `06-功能设计.md`) are
from the 2026-06-23 English rename; `docs/design/archive/README.md` says archive
files deliberately keep old names, but these citations are in **live** docs.

### 6.2 Point-in-time reports belong with the other point-in-time reports

`docs/code-review-e2e-efficiency.md` and `docs/data-audit-and-remediation-plan.md`
are both dated 2026-07-10 and are the same genre as everything in
`docs/design/archive/` — a snapshot of a moment, not a maintained spec. Move
them there and add rows to its table.

### 6.3 Design-doc numbering

- `06` and `07` each have two documents (`06-bid-flow-v2.3-rework` /
  `06-functional-design-v2`, `07-procurement-list-category-recognition` /
  `07-technical-design-v2`). Known, disambiguated by slug. **Leave alone** —
  renumbering would break every citation in the tree for no gain.
- `35` is unused. Leave the gap; do not backfill.
- This document takes `43`.

### 6.4 The two TODOs

Root `TODO.md` (engineering / tech debt) claims 「最后更新：2026-06-23」 in its
header while its last commit is 2026-08-22. `docs/TODO.md` (product / UI /
customer feedback) is frozen at v0.2.1 / 2026-05-29. The split itself is stated
in the root file and is fine; both headers need to become true, or
`docs/TODO.md` needs an explicit "superseded, see HANDOFF" banner.

### 6.5 May-2026 documents — needs a call, not a default

`docs/DEMO.html`, `docs/DEMO.md`, `docs/build-pptx.js`, `docs/capture-matrix.js`,
`docs/capture-screenshots.js`, `docs/TESTING.md`, `docs/USER_MANUAL.md` all date
from 2026-05-20…25 and describe a UI that has since been rebuilt twice
(design/24, design/27). `docs/TESTING.md` still documents the deleted OCR
chain. **Whether these are archived or updated depends on whether they are still
shown to the customer** — that is a product question, deferred.

### 6.6 `HANDOFF.md`

1477 lines / 103 KB, 28 sections spanning 2026-08-11 → 08-24, newest first with
invalidation banners on the stale ones (the convention set on 2026-08-22). It
works, but it is one file. Splitting is optional and carries a real cost:
handoff value comes from a single place a successor reads top to bottom.
**Recommendation: leave it, revisit past ~2000 lines.**

## 7. What this round does not touch

- **`docs/data/` layout.** 94 of the 98 flat CSVs are byte-identical to
  `docs/data/raw/2026-06-23/csv/` (the four 配电箱 files differ), which is 18.92
  MB of duplication. But `docs/data/README.md` and `design/11` §3 both declare
  the flat layer a frozen legacy-v0 snapshot. Changing that is a governance
  decision under `design/11`, not cleanup. Noted here so the next person knows
  it was seen and deliberately left.
- **`docs/test1/` (96.22 MB)** including the 54.85 MB `.zip` and the `.7z` —
  raw archives, part of the deferred corpus round.
- **The three `.docx` contracts** — deferred by the user.
- **Root-directory local clutter** (103 untracked files: 58 `tmp_p*.html`, 18
  `*.log`, 0.6 MB total). **All already gitignored — not in the repository.**
  Deleting them is a local convenience, not a repository change.

## 8. Order and verification

| Phase | Verification |
|---|---|
| 0 — commit the fixture files under the new names + fix the 12 stale references | `git stash -u` the untracked fixtures → run the four affected test modules → confirm they fail → restore → confirm they pass. This is the red/green proof that Phase 0 fixes a real breakage rather than describing one. |
| 1 — evidence-backed deletions | Full backend suite + `npm test` + `vue-tsc`. Removing `ocr_snapshots/` must not move any count. |
| 2 — `scripts/archive/` | `git mv` only, no edits. Grep the tree for each archived filename to confirm nothing imports it. |
| 3 — documentation | Re-run the dead-reference scan from §6.1; it must return empty. |

Each phase is a separate commit. Phase 0 stands alone — it is a fix, and it
should not be buried inside a cleanup commit.
