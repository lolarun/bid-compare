# Best-Practice Review — Scope and Checklist

> **Status — review completed, remediation in progress, 2026-08-11.** The scope
> below was drafted 2026-08-10 before the review ran; the review itself (findings
> A1-A4/B1-B3/C1-C6/D1-D5/E1-E4/F1-F4/G1 + naming supplement N1-N10) happened in a
> separate session and its findings arrived pasted into chat, not as a file. Six
> remediation batches landed 2026-08-11 (commits `7d2adb0`..`17c8a68`): C1/D1,
> F1/F2/F4 (legacy physical deletion), C2/D2-D5, G1, N2/N3/N7, and the `services/`
> package reorg. **E1-E4 and B3 were deliberately deferred** to their own round
> (too large to fold into the batch that also did C/D/F/G/N) — see each batch's
> commit message for what was and wasn't done and why. All of E1-E4 and B3 are
> now resolved (see the entries below); B3's compat-period tail step (drop
> `supplier_id`, flip frontend joins to `submission_id`) is explicitly left
> for a later round once the new key has had real usage time, not deferred by
> oversight.
>
> **E1/E2/E3 — resolved 2026-08-11**: `apps/api/core/errors.py` introduced
> (`DomainError`/`ValidationError`/`NotFoundError`/`ConflictError`/
> `ReviewRequiredError` + `register_exception_handlers`); services now raise
> typed domain errors instead of `fastapi.HTTPException` (E2). Status-code
> policy narrowed per-site after re-reading each one — 2 of the review's 4
> "no confirmed session" sites turned out to be GET/query endpoints where 404
> is correct REST semantics, not the same "write blocked" semantic as the
> other 2 (E1). `/tender-list/match`'s quality-gate response moved 409→422;
> its catch-all no longer leaks `type(e).__name__` to the client (E3). The
> other three E3 sub-items (ValueError→status-code mapping, bare-string vs
> dict `detail`, `export.py` "almost no error contract") were each read
> endpoint-by-endpoint and judged **not a real defect** — see commit
> `e401212` for the per-item reasoning; no code changed for those three.
>
> **E4 — Tier 1 + Tier 2 resolved 2026-08-11**, per a user-specified 3-tier
> split by frontend-coupling depth rather than a flat pass over all 22
> `analysis.py`/`quotes.py` endpoints lacking `response_model`:
> - **Tier 1** (batch, shallow verify — route builds the dict itself, frontend
>   already has a TS type or is unused/internal): 15 endpoints wired in one
>   commit (`refresh-baselines`, `bid-alignment/groups/{id}` DELETE,
>   `anchor-review` GET/confirm/item-confirm/bulk-confirm/finalize,
>   `tender-list/preview`/`reconcile`/`confirm`/`current-sessions`/
>   `current` (GET+DELETE)/`versions`, `compare-state`). One real hidden-coupling
>   hit: `GET /anchor-review`'s `bid_quote_line_id` isn't in the frontend TS
>   type but `test_bql_e2e.py` asserts on it — added to the schema.
> - **Tier 2** (named endpoints, same verification depth, with an explicit
>   stop-loss rule): `tender-list/llm-fill`, `tender-list/match`,
>   `/api/quotes/batch-confirm`. The user predicted these would trip the
>   stop-loss (particularly llm-fill's 19-key response); verification found
>   the opposite — all three have **zero real frontend consumers** for their
>   extra/undeclared fields (llm-fill's `tenderListLlmFill` wrapper is called
>   from nowhere in `apps/www/src`; `tender-list/match`'s `readiness_list`/
>   `per_supplier_stats` and `batch-confirm`'s `checksum`/`integrity`/
>   `missing_total_rows`/`not_quoted_rows`/`not_quoted_detail` are all unread
>   by the strictly-typed `.vue` call sites). No stop-loss trigger; all three
>   got full schemas in one commit. `batch-confirm`'s prior `response_model=dict`
>   was replaced with a real schema — audit/gate fields kept even though unread
>   today, per CLAUDE.md §4's evidence-chain requirement, not dropped to match
>   the narrower existing TS type.
> - **Tier 3** (`bid-matrix`, `bid-matrix/save`, `bid-matrix/versions`,
>   `bid-matrix/versions/{id}`, `bid-matrix/versions/{id}/approve`): **not
>   scheduled as its own round** — merged into a future **B3** (identity-key
>   rename) pass. `SupplierCell.supplier_id`/`MatrixTotal.supplier_id` are the
>   exact wrong-named fields B3 exists to fix; adding `response_model` now
>   would cement the wrong contract and force the frontend through two
>   migrations instead of one.
>
> **E4 residue — resolved 2026-08-11.** The 4 `quotes.py` `response_model=dict`
> placeholders wired to real schemas (`apps/api/schemas/quote.py`:
> `QuoteListResult`, `QuoteBatchListResult`, `QuoteStatsResult`,
> `ArchivePricesResult`). Correction to this section's own premise: the frontend
> API surface lives across two files, not one — `apps/www/src/api/client.ts`
> holds the TS interfaces (`Quote`, `QuoteStats`, `PaginatedResponse<T>`),
> `apps/www/src/api/index.ts` holds the actual call sites (`quoteApi.*`); the
> shallow-verify method had to check both, not just `client.ts`.
> - `GET /api/quotes`: `history/IndexView.vue` is a real consumer with its own
>   local `QuoteRow` type — confirmed it declares (and the table's `dataIndex`
>   columns actually read) exactly `material_name`/`spec`/`supplier_name`/
>   `unit`. `category`/`profession`/`project_name` are flattened by the route
>   but read by nothing in that component today — kept in the schema anyway
>   per the Tier 2 evidence-chain precedent (computed-but-unread ≠ droppable).
> - `GET /api/quotes/batches`: frontend's inline response type in `index.ts`
>   already matched the route's dict shape field-for-field; `batches/IndexView.vue`
>   confirmed as the real consumer via its own `BatchRow` interface.
> - `GET /api/quotes/stats`: zero real frontend consumer — `quoteApi.stats` is
>   only exercised by a URL/params-assertion unit test, not called from any
>   `.vue`. Same shape as Tier 2's llm-fill finding; schema written from what
>   the route computes, not from any frontend consumption.
> - `POST /api/quotes/archive-prices`: zero frontend binding at all (no
>   `quoteApi.archivePrices` in `index.ts`). The 3-state `status` literal
>   (`archived`/`partially_archived`/`no_eligible`) and exact field-by-field
>   behaviour were cross-checked against `test_bql_e2e.py`'s assertions, which
>   is the only real consumer of this contract today.
>
> Verification: full backend suite (`apps/api/tests` + `tests`, both testpaths —
> `pyproject.toml`'s `testpaths` config is silently overridden by explicit CLI
> paths, see the earlier `services/` reorg lesson) → 754 passed, same 4
> pre-existing failures, failure text byte-identical to baseline (not just the
> count). Frontend `vue-tsc -b` clean.
>
> **B3 — resolved 2026-08-11.** Confirmed the exact bug on read: `build_anchor_matrix`
> (`services/matrix/bid_matrix.py`) keys every column by `col_id` — `BidSubmission.id`
> when `use_submission_mode` (the modern §7 path), `Supplier.id` in the legacy
> fallback — but serializes it under the literal key `"supplier_id"` in `SupplierCell`,
> `MatrixTotal`, and (via `bid_recommendation.py::_compute_recommendation`) every
> `supplier_evaluation` entry, `price_preferred_candidate`, and
> `common_comparable.supplier_ids`. `SupplierLabel` was already correct (generic
> `id` for the column key, separate `supplier_id` for the real FK) — just not yet
> in the Pydantic schema or TS type.
>
> Fix, staged for a compat period (new key added, old key's *value* left
> unchanged — not a silent semantic flip):
> - Added `submission_id: int | None` next to `supplier_id` on `SupplierCell`,
>   `MatrixTotal`, `SupplierLabel`, and the `supplier_eval`/`common_comparable`
>   dicts in `bid_recommendation.py`. Populated = `col_id` when
>   `use_submission_mode`, else `None`. `supplier_id` keeps holding `col_id`
>   unchanged (mislabeled but functionally identical to before) — every
>   existing frontend join (`cell.supplier_id === label.id`) still works
>   without modification during the compat window.
> - Also closed 4 fields `SupplierCell` was silently going to drop once
>   `response_model` landed (found by diffing the schema against
>   `_build_cell_for_supplier`'s actual dict, same discipline as E4):
>   `unit`, `supplier_qty`, `item_canonical`, `tax_basis_assumed`.
> - Wired `response_model` on all 5 deferred Tier-3 endpoints: `POST
>   /bid-matrix` → `BidMatrixResult` (already fully declared at the top level
>   from earlier work; only the identity-key + cell-field gaps above were
>   missing). `POST /bid-matrix/save`, `GET /bid-matrix/versions`, `GET
>   /bid-matrix/versions/{id}`, `POST /bid-matrix/versions/{id}/approve` got
>   new lightweight wrapper schemas — `matrix_json`/`readiness_json`/
>   `excluded_rows_json`/`supplier_ids_json` stay untyped `dict`/`list`
>   (persisted snapshot, read/written whole, not field-consumed — same
>   reasoning as E4's audit-dict fields).
> - Frontend (`apps/www/src/api/client.ts` + `views/compare/IndexView.vue`):
>   completed `SupplierCell`/`MatrixTotal`/`SupplierLabel` TS types to match
>   the backend exactly, added `SupplierEvaluation`/`CommonComparable`/
>   `NonPriceFactor` and the missing `BidMatrixResult` recommendation fields
>   (`recommendation_level`/`award_mode`/`committee_required`/`price_ranking`/
>   `risks`/`evaluation_policy`/`price_preferred_candidate`/
>   `supplier_evaluation`/`common_comparable`/`non_price_factors`/
>   `comprehensive_recommendation_status`). Removed the `matrixResult.value as
>   unknown as Record<string, any>` escape hatch in `matrixSummary` and the 6
>   per-field `as any` casts on `matrixTotals.find(...)` — all now type-check
>   against the completed interfaces with no casts.
> - **Deliberately not done today** (needs real compat-period elapsed time,
>   not a same-session step): flipping `supplier_id`'s value to the true
>   supplier FK, migrating frontend joins off `supplier_id` onto
>   `submission_id`, and removing `supplier_id` entirely. `common_comparable`/
>   `price_ranking` etc. stay untyped `dict`/`list[dict]` in the Python schema
>   (loose containers don't strip unknown keys, so the new `submission_id`
>   key rides along safely without a schema change).
> - **Out of scope, not touched**: `_build_material_row`/`_finalize_row` in
>   `bid_matrix.py` are dead code (zero callers anywhere — confirmed by grep)
>   left over from a pre-anchor-matrix code path; not part of the identity-key
>   bug since nothing calls them. `bid_matrix_save`'s request-body
>   `supplier_ids_json` (persisted snapshot input, not a response identity
>   key) was not audited for the same naming issue — out of the pinned scope.
>
> `apps/api/tests + tests -q`: 754 passed, 4 failed (existing, unrelated,
> byte-identical to baseline), 1 skipped, 7 deselected. `apps/www`
> `type-check` (vue-tsc -b) and `test:unit` (vitest, 42 tests / 4 files): both
> green.
>
> **N1 — resolved 2026-08-11**: `vl_direct.py` → `vl_quote.py` (symmetric with
> `vl_tender.py`; "direct" was a contrast name against legacy, which no longer
> exists). Persisted labels (`parser_mode`/`input_mode`/`recognizer` = `"vl_direct"`)
> are unchanged — no data migration, see the module's own docstring. The other
> N-items' "record only" dispositions from §2.1 below are unchanged.
>
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
| `_SLOTS` tax patterns | `intelligence/vl_quote.py (formerly vl_direct.py)` | Grew reactively (`excl` → `ex_tax` → `pre_tax` → …). The list can never be complete; the real defence is `has_price_column`. Check that the fallback, not the list, is what production relies on. |
| `INTEGRITY_*`, `SEQ_*`, `BLOCK_*` thresholds | `core/domain_config.py` | All derived from the same seven documents. Each carries its derivation in a comment — verify the comment still matches the value, and that none silently became a magic number. |
| `CHECKSUM_BLOCK_DELTA_RATIO` | `core/domain_config.py` | Known wrong *shape* (ratio, should be per-row). Design in `docs/design/20`; instrumentation already shipped, threshold not yet changed. |
| `ORIENT_PROBE_MAX_EDGE_PX` | `intelligence/vl_quote.py (formerly vl_direct.py)` | Derived from one offline baseline (scale 0.30 → 253–357px, rounded up to 400). Changing it invalidates the accuracy baseline; verify that is stated where someone would look before editing. |
| Prompt rules 1–4 | `vl_quote.py (formerly vl_direct.py)`, `vl_tender.py` | Must contain no real supplier/project/file names and no sample-specific column order (`.claude/rules/recognition.md`). Re-verify after the tender prompt was added. |

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
- `vl_quote.py (formerly vl_direct.py)` now carries both shared machinery and quote specifics; `vl_tender.py`
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
