# Archived scripts

These are **kept for reference, not maintained, and not part of the current
recognition/alignment chain**. Mirrors the convention in
`archive/design/README.md`. See `archive/design/43-repository-cleanup.md`
§5 for the rule that put them here: `scripts/` root keeps what is re-runnable
on new input; this directory takes what is bound to a past incident, a
retired chain, or a retired model.

| Group | Scripts | Why archived |
|---|---|---|
| Bound to specific records | `audit_sub1819.py`, `retarget_sub1819.py`, `repair_proj63_sub1719.py`, `dump_sub18_bqls.py`, `extract_ocr_html.py`, `crop_missing_items.py`, `crop_p11_p12.py`, `audit_gate_misfire.py`, `_dump_gate_cells.py`, `verify_supplier_name_fix.py`, `check_kaishuoxinzheng.py`, `find_pdf_path.py`, `inspect_job_result.py` | Written against one specific project/submission/page in the database or a specific PDF. Not parameterizable to new input without a rewrite; the historical repair is what has lasting value, not re-running the script. |
| One-shot data audits | `_analyze_hist.py`, `_audit_data.py`, `_audit_materials.py`, `_brand_coverage.py`, `_check_blank_unit.py`, `_check_hvac_brand.py`, `_check_supplier_fk.py`, `_inspect_data.py`, `_preview_pdb.py`, `_verify_baselines.py`, `_verify_brands.py`, `_verify_quality.py` | Ad-hoc checks against the historical-price import at the time it happened. Superseded by `docs/spec/FUNCTIONAL.md` §10's ongoing governance process (originally `docs/design/11-historical-price-governance.md`, now `archive/design/11-historical-price-governance.md`). |
| Retired chains / retired models | `test_two_stage.py`, `test_ocr_pipeline.py` (the per-page OCR→HTML→TableGrid chain, physically deleted 2026-08-11 per `.claude/rules/recognition.md`), `bench_llm_models.py`, `compare_models.py`, `compare_glm_vs_sf32b.py`, `detail_compare.py`, `test_glm.py`, `test_qwen_ocr.py` | Target a recognition path or model that is no longer in production. |
| Old API smoke scripts | `test_alignment_db_mode.py`, `test_bid_alignment.py`, `test_bid_alignment_apply.py`, `test_bid_matrix_alignment.py`, `test_bid_status.py`, `test_bid_status2.py`, `test_bid_status_dashboard.py`, `test_brand_recommend.py`, `test_excel_import.py`, `test_naming_rules.py`, `test_postprocess.py`, `test_single_supplier.py`, `test_tender_ext_attrs.py`, `test_e2e_anchor.py`, `test_e2e_enhance.py`, `test_e2e_alignment.py`, `test_e2e_tabular.py`, `test_e2e_experiment_a.py`, `test_e2e_llm_fill.py`, `test_e2e_llm_fill_batch.py`, `add_bid_status.py`, `create_test_excel.py` | Manual API-smoke scripts from before `apps/api/tests/` had equivalent coverage. Named `test_*` but never collected by pytest (`testpaths` excludes `scripts/`) — the name promised a guarantee nothing enforced. |
| Completed one-off migrations | `migrate_v1_to_v2.py`, `convert_contract_md_to_docx.py` | The migration or conversion they performed is done; the target schema/format has since moved on. |

**Naming rule this enforces**: no `test_*.py` in `scripts/` root going forward —
that prefix belongs to `apps/api/tests/` and `tests/`, the only directories
`pyproject.toml`'s `testpaths` actually collects.
