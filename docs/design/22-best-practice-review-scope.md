# Best-Practice Review — Scope and Checklist

> **Status — scope definition, 2026-08-10. Review not yet run.**
> Requested by the user after the VL-direct migration, to clean up and standardise
> the code before further feature work.
>
> Layers in scope: `apps/api/services/`, `apps/api/routes/`, `apps/api/intelligence/`.

## 1. Why this document exists before the review

Two reasons to write the scope down rather than start reviewing:

- A review without a stated scope silently becomes "whatever the reviewer noticed",
  and the parts nobody looked at are indistinguishable from the parts that passed.
- Several items below are **known suspects already found during the migration**.
  Recording them now prevents the review from re-discovering them as if they were new,
  and prevents them from being quietly dropped.

## 2. Cross-cutting checks

### 2.1 Parameterisation must generalise, not fit the current samples

Raised by the user 2026-08-10, and it is the right lens: every parameter, threshold,
slot table and prompt rule introduced during this migration was derived from **seven
cable quotes and one valve tender**. A parameter that merely encodes those samples is
worse than a hardcoded value, because it *looks* configurable.

For each parameterised thing, the review must answer: *what happens on a category we
have never seen?*

Known suspects, to audit item by item:

| Thing | Where | Suspicion |
|---|---|---|
| `TENDER_SLOTS["pressure"]` | `intelligence/vl_tender.py` | 工作压力 is valve-specific. 桥架 needs 表面处理/板材厚度/荷载等级; 风机盘管 needs 制冷量/风量. None are slots. Mitigation verified: unmatched columns survive in `extra_fields` + `raw_cells`, so data is not lost — but they never reach `TenderAnchor`'s typed fields. The valve-centricity is inherited from `TenderAnchor` itself (it has `pressure`, `materials`, valve `canonical`), not introduced here. Decide whether `EXTENDED_ATTR_SCHEMAS` (`core/config.py`) should drive slots per category instead. |
| `_MATERIAL_PREFIXES` | `intelligence/vl_tender.py` | Uses the **parent-column prefix** (材质) rather than enumerating child names (阀体/阀芯…), which is the generic form. Verify it survives a category whose parent column is named differently. |
| `_SLOTS` tax patterns | `intelligence/vl_direct.py` | Grew reactively (`excl` → `ex_tax` → `pre_tax` → …). The list can never be complete; the real defence is `has_price_column`. Check that the fallback, not the list, is what production relies on. |
| `INTEGRITY_*`, `SEQ_*`, `BLOCK_*` thresholds | `core/domain_config.py` | All derived from the same seven documents. Each carries its derivation in a comment — verify the comment still matches the value, and that none silently became a magic number. |
| `CHECKSUM_BLOCK_DELTA_RATIO` | `core/domain_config.py` | Known wrong *shape* (ratio, should be per-row). Design in `docs/design/20`; instrumentation already shipped, threshold not yet changed. |
| `ORIENT_PROBE_MAX_EDGE_PX` | `intelligence/vl_direct.py` | Derived from one offline baseline (scale 0.30 → 253–357px, rounded up to 400). Changing it invalidates the accuracy baseline; verify that is stated where someone would look before editing. |
| Prompt rules 1–4 | `vl_direct.py`, `vl_tender.py` | Must contain no real supplier/project/file names and no sample-specific column order (`.claude/rules/recognition.md`). Re-verify after the tender prompt was added. |

**Fixed during scoping, recorded here so the review does not re-find it:** slot
assignment was not exclusive — 「规格型号」 matched both `spec` and `model`, filling two
fields from one column. Exclusivity added; verified to produce **identical mappings on
all seven real quote headers**, so it removes an ambiguity without changing behaviour.

### 2.2 Invariants from CLAUDE.md §4

Check each is actually enforced at the earliest layer, not re-derived downstream:
identity (`anchor_id` / `submission_id` / `supplier_id` / `material_id` never conflated);
fact lifecycle (draft → confirmed); quality tiers gating storage/alignment/recommendation;
isolation of test/demo data; one business result shared by page/export/AI; LLM explains
only; thresholds centralised.

## 3. Per-layer checks

### 3.1 `routes/`

- Routes should carry HTTP concerns only — auth, params, transaction boundary, response
  mapping. Any business rule found in a route is a finding.
- Response models: several endpoints return bare `dict` (`quotes.py:batch-confirm`,
  `analysis.py:tender-list/*`). Decide whether that is acceptable or should be typed.
- Error contract consistency: 400 vs 409 vs 422 usage varies across the alignment and
  confirmation endpoints; catalogue and normalise.
- Auth coverage: `test_intake_routes.py` sends no token while the router carries a global
  auth dependency — verify whether that test is passing for the wrong reason.

### 3.2 `services/`

- Seven authoritative services exist (Session / SubmissionScope / EvaluationPolicy /
  BidMatrix / BidExport / Alignment / QuoteConfirmation). Verify nothing bypasses them.
- `bid_matrix.py` internal split was deferred (§10.3 third batch) — still outstanding.
- Duplicate logic between `quote_confirmation_service` and `submission_eligibility`
  (price coverage was deliberately left to the match gate — confirm it stayed that way).

### 3.3 `intelligence/`

- **Legacy surface after archival.** The quote branch is archived but `table_recognizer`,
  `table_parser`, `page_classifier`, `adaptive_tiler`, `snapshot_provider`, `aggregator`
  remain, now serving the tender path (and `_run_batched` for both). After the VL tender
  recogniser lands, re-audit which of these still has a live caller.
- `vl_direct.py` now carries both shared machinery and quote specifics; `vl_tender.py`
  holds tender specifics. Decide whether the shared part deserves its own module.
- `pipeline.py` retains quote-legacy helpers (`_get_quote_adapter`, `_quote_detect_pages`,
  `_quote_extract_meta`, `_quote_prompt_for_mode`, `_assign_source_ref_from_grids`) —
  check for dead code now that the quote legacy branch is gone.
- Known gaps from `docs/design/21` §2 that are code-level, not design-level:
  `supplier_name` always empty on the VL path; `declared_total` never supplied;
  `metadata["doc_meta"]` never set by anything (pre-existing, affects both paths);
  `parser_mode` dropped by `_draft_to_quote_response`'s whitelist;
  `input_mode="vl_direct"` missing from the frontend union type.

## 4. Explicit non-goals

- Not a bug hunt for recognition accuracy — that is the C layer's job
  (`test_cable_accuracy_e2e.py`), and accuracy claims need fresh runs, not code reading.
- Not a frontend review. The user sequenced frontend work after the API and test suite
  are stable.
