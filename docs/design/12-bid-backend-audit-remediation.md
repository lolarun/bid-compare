# Independent Backend Audit and Remediation Proposal for Tender-Comparison E2E

> **Status — audited 2026-06-23.** Partially implemented. All five P0 findings (§3) are now fixed in code; P1-1/P1-2/P1-3+P1-4/P1-5/P1-6 and P2-1/P2-3 are done; structural-governance items (§10/§11) remain pending, except §11.2 soft-FKs (done via migration `0004_soft_fk`) and parts of §11.5.
> _Originally written 2026-06-22. English translation of the Chinese original; now the authoritative version._

> Audit date: 2026-06-22
> Scope: tender-list confirmation, quote confirmation, alignment, matrix, evaluation explanations, export, historical prices, and invited-supplier recall.
> Nature: read-only code audit; this report did not modify production logic, the database, or match results.

## 1. Audit method

This round did not take Claude's earlier conclusions as a premise; instead it verified backward from the real call path:

1. route entry points and what the frontend actually calls;
2. session / submission / supplier identity resolution;
3. persistence from quote confirmation to `BidQuoteLine`;
4. anchor match, review matrix, bid matrix, export;
5. evaluation policy, AI insight, and historical-price services;
6. test coverage and uncovered branches.

It also reviewed the repository rule files against the official Claude Code best practices. The official guidance recommends keeping `CLAUDE.md` concise, specific, and structured, targeting roughly 200 lines or fewer; use path-scoped `.claude/rules/` for requirements that vary by code domain, rather than continually piling all historical decisions into the root file.

References:

- <https://code.claude.com/docs/en/memory>
- <https://code.claude.com/docs/en/best-practices>

## 2. Overall conclusion

The main E2E now has a runnable skeleton, but the backend remains in a state where a "new-submission main path + legacy supplier/alignment compatibility path + heavy in-route orchestration" coexist.

The thing to watch most is not file length itself, but these three classes of business risk:

1. **the legacy path is still reachable and has a deterministic runtime error**;
2. **evaluation, historical-price, and invited-supplier evidence bases have not fully converged onto authoritative services**;
3. **submission identity, audit evidence, and the migration mechanism are still in a half-migrated state**.

Directory splitting should serve these boundaries, not be a big refactor for the sake of "tidy files". The recommendation is to fix the P0 behaviors first, then do the service split.

## 3. P0 findings

### P0-1 Legacy matrix path has a deterministic `NameError`

(corrected 2026-06-23: fixed — the legacy `build_bid_matrix()` / `_build_alignment_row()` path and the `first_qt` reference have been removed from `bid_matrix.py`; no `first_qt` / `first_data` / `_build_alignment_row` symbols remain.)

Location: `apps/api/services/bid_matrix.py:369`

The `_build_alignment_row()` local variable is `first_data`, but the return value references a nonexistent `first_qt.material_id`. Once this branch processes a confirmed alignment group it fails outright.

This is not dead code: `build_bid_matrix()` is still called by the compatibility paths in `analysis.py` and `export.py`. The current passing tests cannot prove this branch is safe, because the existing 71 relevant tests do not hit this specific branch.

Recommendation:

- if the legacy matrix has no business value, explicitly retire the entry point and delete it;
- if compatibility is needed, fix the variable immediately and add a regression test for "a confirmed alignment group exists";
- do not let old and new matrices keep coexisting unlabeled in production.

### P0-2 Evaluation policy wrongly tags unknown projects as a known policy

(corrected 2026-06-23: fixed — `get_evaluation_policy(project_id)` now returns `UNKNOWN_EVALUATION_POLICY` (`method="unknown"`, `award_mode="unknown"`, `weights=None`, `final_decision_requires_committee=True`) for all projects until tender-document policy persistence exists.)

Location: `apps/api/services/evaluation_policy.py:39-44,81-87`

`get_evaluation_policy(project_id)` completely ignores `project_id`; all projects return:

- `reasonable_low_price`
- `single_supplier`
- a fixed set of eight factors
- committee decision

"No automatic award" is conservative, but claiming that the tender document uses the reasonable-low-price method and single-supplier award is still fabricating a policy fact. For projects whose policy has not been extracted from the tender document or manually confirmed, the correct state is `method=unknown`, `award_mode=unknown`, `weights=null`, `committee_required=true`.

Recommendation: establish an `EvaluationPolicyService` that prefers a confirmed tender policy; when missing, return unknown and let the AI only explain the gap rather than fill it in for the system.

### P0-3 Invited-candidate recall can still be polluted by invalid historical quotes

(corrected 2026-06-23: fixed — the candidate recall query in `supplier_recommend.py` now applies `valid_quote_filters()`, and the cold-start pool is restricted to `Supplier.merge_status == "active"`; `statistics.recommended_brands` now joins `Supplier` and applies `valid_quote_filters()`.)

Location: `apps/api/services/supplier_recommend.py:234-253`

Supplier-score aggregation uses `valid_quote_filters()`, but the upfront candidate-recall query does not, nor does it restrict to active suppliers. The result is:

- test / E2E / excluded quotes can pull a supplier into the candidate set;
- merged/inactive suppliers can enter candidates;
- even if scored low afterward, the candidate identity itself is already polluted.

Location: `apps/api/services/statistics.py:189-194`

`recommended_brands` reads `Quote.brand` directly, with no `Supplier` join and no `valid_quote_filters()`. This writes test quotes, invalid suppliers, and quote-stated brands back into the material's recommended brands.

Recommendation:

- route candidate recall, statistics scoring, and brand evidence uniformly through the historical-data business service;
- suppliers must be active;
- grade brands into three tiers: authorization/agency evidence, historical-procurement evidence, and quote self-report;
- a brand in quote text may only be weak evidence and cannot directly establish an agency relationship.

### P0-4 Documents over 200 pages are silently truncated and reported as complete

(corrected 2026-06-23: fixed — `table_recognizer.py:389-392` now computes `truncated = rendered_pages < actual_page_count`, uses the real `actual_page_count` as `total_pages`, and passes `truncated`/`rendered_pages` to `compute_quality`; `extraction_draft.py:348-349` adds `document_truncated` to the blocking list (BLOCKED).)

Locations:

- `apps/api/intelligence/document_loader.py:24,77-82`
- `apps/api/intelligence/table_recognizer.py:389-391`

`MAX_PAGES_UNLIMITED` is actually 200. `to_thumbnails()` takes `min(len(pdf), cap)` of the real page count, and the recognizer then treats the rendered count as `total_pages` without passing `truncated=True`.

So a 201+ page PDF is silently treated as a complete document. This violates the completeness gate.

Recommendation: have `DocumentLoader` return `{actual_pages, rendered_pages, truncated}`; the quality report uses the real page count and goes BLOCKED on truncation.

### P0-5 Production prompt contains real supplier and brand samples

(corrected 2026-06-23: fixed — `TENDER_BRANDTABLE_PROMPT` now uses fully fictional placeholders (e.g. `ALFA 阿法`, `VEGA 威盖`, `ORION 猎户`, and fictional company names); the real KITZ/WATTS/BERMAD/凯硕/绵存/泰科龙 samples have been removed.)

Location: `apps/api/intelligence/prompts.py:168-179`

The `TENDER_BRANDTABLE_PROMPT` example contains real project data such as KITZ, WATTS, BERMAD, 凯硕, 绵存, 泰科龙. This introduces sample bias into the model and also violates the repository's "do not hardcode against existing files" requirement.

Recommendation: replace with fully fictional placeholder brands and suppliers, and use the real samples only as test fixtures.

## 4. P1 findings

### P1-1 Routes and services are overloaded

(done per TODO.md — the seven authoritative services were extracted: `TenderSessionService` / `SubmissionScopeService` (`bid_submission_resolve`) / `QuoteConfirmationService` / `AlignmentService` / `BidMatrixService` / `EvaluationPolicyService` / `BidExportService`. corrected 2026-06-23: `routes/analysis.py` is still NOT split — currently ~2470 lines — see TODO.md §0.1; the `match` and `llm-fill` route logic is still in the route, see TODO.md §0.2.)

Current sizes:

| File | Lines | Main problem |
|---|---:|---|
| `routes/analysis.py` | 2599 | session, match, review, persistence, finalize, and state recovery are mixed in one route module |
| `routes/quotes.py` | 903 | `batch_confirm` does validation, basis derivation, supplier handling, and persistence all at once |
| `routes/export.py` | 606 | scope/session resolution is coupled with export-format orchestration |
| `services/bid_matrix.py` | 1513 | legacy matrix, anchor matrix, review matrix, baseline, recommendation, and evaluation statistics coexist |
| `intelligence/table_recognizer.py` | 1668 | acceptable, but needs stable internal boundaries to avoid further growth |

Claude's judgment about "large functions, blurred boundaries" holds. But the claim that "pages and export each compute an entirely separate matrix" is overstated: the anchor main path already jointly calls `build_anchor_matrix()`; the real duplication is in session/scope resolution, finalization, and format mapping.

Recommendation: split by business boundary:

- `TenderSessionService`
- `SubmissionScopeService`
- `QuoteConfirmationService`
- `AlignmentService`
- `BidMatrixService`
- `EvaluationPolicyService`
- `BidExportService`

Extract authoritative services and contract tests first, then thin the routes; do not move all files at once.

### P1-2 Submission-identity migration not fully closed out

(done per TODO.md — submission-identity authoritative resolution is complete; `resolve_active_submissions()` does not union supplier scope when explicit `submission_ids` are given.)

The new main path correctly uses `BidSubmission.id`, and `resolve_active_submissions()` does not union supplier scope when `submission_ids` are explicit.

But the following debts remain:

- `/anchor-review` retains the legacy supplier-id path;
- schema and matrix variables are still largely named `supplier_ids`, while sometimes actually carrying submission IDs;
- comments call `supplier_ids` "deprecated/ignored", yet the code still explicitly enables the legacy supplier scope;
- the legacy path cannot naturally express an unknown supplier or the same supplier bidding multiple times.

Recommendation: new API contracts should expose only `submission_ids`; mark the old entry points as deprecated with monitoring, and delete them once no callers remain. Responses should provide both `submission_id` and `supplier_id`; never let one field carry dual semantics.

### P1-3 Recognition audit fields are persisted as weakly typed JSON

(done `bdaa890` per TODO.md — `BidQuoteLine.row_type VARCHAR(32)` added (migration 0003) with vocabulary consolidation, and `BidQuoteLine.updated_at` added (migration 0002). The remaining gap — field-level before/after for manual corrections and an append-only corrections table — is still open pending a corrections API.)

Claude's claim that source and validation info "are not persisted at all" is inaccurate. `routes/quotes.py:673-703` already writes source_ref, the original tax-inclusive/exclusive values, validation_flags, raw_qty, and suggested_qty into `extraction_meta`.

The real gaps are:

- `row_type` and `corrections` are not fully persisted;
- source_ref, flags, and manual corrections are all weakly typed JSON;
- there is no field-level before/after value and operator;
- BQL has no row-level `updated_at` audit semantics.

Recommendation: structure the high-frequency quality fields, keep JSON as the raw audit bundle; build a separate append-only change log for manual corrections.

### P1-4 OperationLog is insufficient for field-level audit

(done `bdaa890` per TODO.md — `operation_logs.payload JSON` added (migration 0003); `services/audit.py:write_domain_event()` provides structured `{event_type, identity, before, after, meta}` payloads at seven instrumentation points: `bql_confirm / tender_session_confirm / alignment_group_confirm / alignment_item_confirm / alignment_bulk_confirm / alignment_finalize / llm_fill_persist`.)

Location: `apps/api/models/operation_log.py:9-19`

It currently has only user/module/action/target/result/remark/time, with no structured before/after and no project/session/submission/row identity.

Recommendation: add domain audit events — confirmation, correction, exclusion, re-match, finalize, and historical import — each recording a structured payload.

### P1-5 Current-session query basis is inconsistent

(done per TODO.md — `get_current_confirmed_session()` was introduced and routes no longer assemble the query themselves.)

Some calls require both `is_current` and `status=confirmed`; some take only `is_current`. This lets an unconfirmed current session be consumed by certain entry points.

Recommendation: wrap `get_current_confirmed_session(project_id, category)`; forbid routes from assembling the query themselves.

### P1-6 Business thresholds scattered

(done 2026-06-22 per TODO.md — `domain_config.py` now holds all ten `MATCH_*` domain thresholds, including `MATCH_SEQUENTIAL_SIM_THRESHOLD`, `MATCH_ARITHMETIC_PASS_THRESHOLD`, `MATCH_PRICE_ARITHMETIC_TOLERANCE`. The env layer (`PAGE_CONCURRENCY` / `PDF_RENDER_CONCURRENCY` / `MAX_PAGES`, etc.) is intentionally kept in env per the three-layer model.)

Match thresholds, checksum, sequence/similarity, page concurrency, and the page-count cap are scattered across route, service, env, and module constants.

Recommendation: distinguish:

- system-resource config: Settings/env;
- domain safety thresholds: a centralized policy/config dataclass;
- project evaluation rules: EvaluationPolicy data.

## 5. P2 technical debt

### P2-1 No versioned database migrations

(done per TODO.md — Alembic introduced; `apps/api/migrations/versions/` holds `0001_baseline` through `0004_soft_fk`; subsequent schema changes go through versioned migrations only.)

`apps/api/core/database.py` relies on `Base.metadata.create_all()` and a large `_ensure_sqlite_schema()`, including ALTER, temp-table rebuild, and PRAGMA operations.

The existing rebuild code has foreign-key/conservation checks — better than an unprotected script — but it is unsuitable for long-term evolution and non-SQLite environments. Recommendation: introduce Alembic, and route all subsequent schema changes through versioned migrations only.

### P2-2 Domain normalization logic duplicated

Valve family/DN logic exists simultaneously in the canonical service, in `anchor_match` coarse classification, and in other prompts/rules. Workable short-term, but long-term it produces three bases — recognition, matching, and historical baseline.

Recommendation: establish a `MaterialIdentityService` that outputs canonical family/DN/PN/unit and preserves evidence/confidence.

### P2-3 Match flow writes `Material.extended_attrs`

(done 2026-06-22 per TODO.md — the 11 lines in `anchor_match.import_and_match()` that wrote canonical data back to `Material.extended_attrs` were removed; `extract_valve_canonical()` is a pure function computed on demand, no DB cache needed.)

The anchor match caches canonical data into the material master data. It does not directly pollute prices, but it gives "read-only bid-comparison" a master-data write side-effect.

Recommendation: store derived canonical data in a separate cache or an explicit enrichment service; do not implicitly write the DB inside the match function.

## 6. Review of Claude's audit

| Claude's view | Review conclusion |
|---|---|
| Route/service files too large, responsibilities mixed | Holds |
| `first_qt` is a runtime bug | Holds, P0 |
| screen/export entirely duplicate the matrix algorithm | Overstated; the main anchor matrix is already shared, duplication is mainly in scope/finalization/format |
| BQL source and flags are not persisted at all | Does not hold; already in extraction_meta, the issue is weak typing and incomplete audit |
| submission/supplier identity migration is a mess | Largely holds, but authoritative resolution of explicit `submission_ids` is already implemented |
| bbox has no positioning at all | Partly holds; bbox is 0, but the tiled path has tile_bbox |
| `repair_project63.py` is a committed script yet imports an untracked module | Cannot hold currently; both are untracked by git — a workspace-hygiene issue, not a clean-checkout breakage |
| Too many workspace scripts/temp assets | Holds; should be categorized/archived or deleted before commit, but must not accidentally delete user data |

### Important issues Claude missed

1. EvaluationPolicy disguises every project as a recognized reasonable-low-price/single-supplier award;
2. supplier-recommendation candidate recall does not uniformly apply the historical-validity filter;
3. recommended_brands uses unfiltered quotes;
4. 200-page truncation reported as complete;
5. production prompt contains real supplier and brand samples.

## 7. Recommended remediation order

### Batch 1: must fix before delivery

(corrected 2026-06-23: all five Batch-1 items are now done — see the P0 inline corrections in §3.)

1. fix or retire the legacy `first_qt` branch and add a regression test;
2. EvaluationPolicy returns unknown when unconfirmed;
3. unify the valid-history filter and active-supplier gate for supplier/brand candidate recall;
4. fix the 200-page truncation report;
5. remove real-sample hardcoding from production prompts.

### Batch 2: stabilize the E2E boundary

(corrected 2026-06-23: items 1–3 and 5 are done per TODO.md; item 4 — pages/export/AI consuming one matrix/evaluation result — remains the open work.)

1. unify a current-confirmed-session service;
2. make new interfaces submission-only and explicitly retire the legacy supplier scope;
3. close out `QuoteConfirmationService` and `BidMatrixService`;
4. have pages, export, and AI consume one matrix/evaluation result;
5. add field-level audit events.

### Batch 3: structural governance

(corrected 2026-06-23: item 1 (Alembic) is done; item 4's untracked-script hygiene is partially done; items 2 and 3 remain pending.)

1. Alembic migrations;
2. a unified MaterialIdentityService;
3. make historical prices, supplier evidence, and brand evidence into business services;
4. clean up untracked temp scripts and duplicate fixtures.

## 8. Verification performed

```text
python -m pytest \
  apps/api/tests/test_compare_integration.py \
  apps/api/tests/test_bql_e2e.py \
  apps/api/tests/test_bid_evaluation.py -q

Result: 71 passed
```

Also ran `python -m compileall -q apps/api`, which passed.

Residual note: the above tests do not cover the confirmed-group branch of the legacy `_build_alignment_row()`, so they do not offset P0-1. (corrected 2026-06-23: P0-1 has since been fixed by removing the legacy branch entirely.)

## 9. Second independent review and convergence of conclusions

> This section is a second cross-review of §3–§6 (item-by-item manual `file:line` checks), aimed at grounding the disagreements between the two independent audits in fact, rather than mutually citing conclusions.

### 9.1 Reviewed and confirmed facts

(corrected 2026-06-23: all rows below describe the state as of 2026-06-22; the underlying defects have since been fixed — see the inline corrections in §3.)

| Conclusion | Review method | Result |
|---|---|---|
| `bid_matrix.py:369` `first_qt` runtime `NameError` | read source + both audits hit it independently | **Holds (three-way agreement), P0-1** |
| `supplier_recommend.py:234-253` candidate recall unfiltered | read source, confirmed the recall query only has `Quote.unit_price > 0`, no `valid_quote_filters()`, no active-supplier gate; cold-start `:270-272` `db.query(Supplier).limit(50)` scoops arbitrary suppliers | **Holds, P0-3** |
| `statistics.py:189-193` `recommended_brands` pollution | read source, confirmed it only takes `Quote.brand` by `material_id`+non-empty brand, no Supplier join, no validity filter, then `commit()` writes back to master data | **Holds, P0-3 extension** |
| `get_evaluation_policy(project_id)` ignores the argument | read `evaluation_policy.py:81-87` | **Holds, P0-2** |
| 200-page silent truncation | read `table_recognizer.py:754` `compute_quality()` does not pass `truncated` | **Holds, P0-4** |

### 9.2 Corrections to the prior round (Claude's) statements

The following are conclusions from the prior-round (Claude) audit that **this round's review disproved or downgraded**, recorded here so later reports do not keep using them as a premise (per Engineering Charter §10 "wrong conclusions must be explicitly retracted"):

1. **"`repair_project63.py` is a committed script yet imports an untracked module, breaking clean checkout" — retracted.** `git ls-files` confirms that both `apps/api/services/rebuild_submission_lines.py` and `scripts/repair_project63.py` are **untracked by git** (`??`). This is a workspace-hygiene issue (to be categorized before commit), not a clean-checkout breakage.
2. **"BQL source/flags are not persisted at all" — downgraded as overstated wording.** `routes/quotes.py:673-703` already writes `source_ref`, original tax-inclusive/exclusive values, `validation_flags`, `raw_qty`, and `suggested_qty` into `extraction_meta`. The real gap is **weakly typed JSON + no `row_type` column + no field-level before/after audit** (see P1-3), not "none at all".
3. **"TableGrid is built yet fed back to the LLM, and html_fallback goes straight to the LLM = §3.3 violation" — retracted.** The current `CLAUDE.md §4` and `.claude/rules/recognition.md` **explicitly permit** "controlled HTML + LLM fallback for complex headers and scanned documents", and forbid reintroducing the rescinded Phase 2 requirement that "all tables must go through deterministic TableGrid direct output". This path conforms to current policy and does not count as a violation.
4. **"Pages and export each compute an entirely separate matrix" — downgraded.** The anchor main path already jointly calls `build_anchor_matrix()`; the real duplication is scope/finalization/format mapping (consistent with P1-1 in this file).
5. The grading of **production prompt contains real samples** is disputed: this report lists it as P0-5, while the second review considers its nature to be few-shot model bias + §10 hardcoding and recommends **grading it P1** (should fix, but does not block data integrity). The final grading is left to the remediation schedule.

### 9.3 Converged P0 (must fix before delivery)

(corrected 2026-06-23: all five items below are now done — see the P0 inline corrections in §3.)

1. fix or retire the `bid_matrix.py:369` legacy `NameError` branch and add a "confirmed alignment group exists" regression test;
2. `EvaluationPolicy` returns `unknown` for unconfirmed projects, not fabricating a policy for the system;
3. unify `valid_quote_filters()` and the active-supplier gate for `supplier_recommend` candidate recall + `statistics.recommended_brands`;
4. fix the 200-page truncation report (`truncated`/BLOCKED);
5. remove real supplier/brand samples from the production prompt (P0/P1 grading TBD).

## 10. Naming and status conventions

> This section answers "are the directory, business-entity, and business-service names accurate, and can statuses be unified". All points were verified by `file:line`. This is structural governance (Batch 3); it does not block delivery, but the conventions should be settled before the service split, so the split does not freeze inconsistent naming in place.

### 10.1 Directory naming

- `intelligence/` is a clear bounded-context name; keep it.
- **`services/` is not a layer, it is a flat junk bag**: 30 files mixing four contexts, with no subpackages. Recommendation: split into subpackages by context and host the authoritative services this file proposes:

| Bounded context | Currently scattered files | Target subpackage / service |
|---|---|---|
| recognition / material-identity normalization | `canonical` `standardize` `enhance` `category_classify` `brand_match` `source_reconcile` | `services/recognition/`; close out into `MaterialIdentityService` (P2-2) |
| tender-comparison main flow | `anchor_match` `bid_matrix` `bid_alignment` `tender_list` `evaluation_policy` `bid_insight` | `services/comparison/`; split out `BidMatrixService` / `AlignmentService` / `EvaluationPolicyService` |
| historical prices / master data | `comparison` `statistics` `scoring` `supplier_recommend` `supplier_resolve` | `services/history/`; close out into `HistoricalPriceService` (P0-3 / rule) |
| ingestion | `import_service` `tabular_ingestion` `document_ingestion` `tender_pdf` | `services/ingestion/` |

- **`routes/analysis.py` is misnamed**: it actually carries the entire comparison workflow (session / match / review / finalize / matrix / insight) and should, along with the P1-1 split, be split into domain routes such as `tender_list` / `anchor_review` / `bid_matrix` / `alignment`.

### 10.2 Business-entity naming

**The most serious one: `TenderAnchor` is repeatedly declared by the rule files as "the unique row axis of the matrix", yet has no ORM entity.** There is no `models/tender_anchor.py`; the anchor exists only as JSON inside `TenderListSession` plus an `anchor_seq = String(20)` (`bid_alignment.py:38`) passed around everywhere. The most core authoritative concept is a string — the structural root cause of why the anchor chain assembles queries from seq everywhere and why the basis is hard to unify. Recommendation: promote TenderAnchor to a first-class entity (or at least a first-class value object + stable ID).

Other entity-naming issues:

| Entity | Problem | Recommendation |
|---|---|---|
| `Quote` vs `BidQuoteLine` | both are "quote lines"; the names do not distinguish "historical-price fact" from "current bid line" — this is the etymology of the `supplier_id`/`submission_id` dual track | `Quote` → `HistoricalPrice` / `PriceRecord` |
| `BidAlignmentGroup` / `BidAlignmentItem` | Group/Item too generic; Group is really "one canonical comparison line", Item is "one quote folded into that line" | `ComparisonLine` / `LineMember` |
| `AlignmentFinalization` vs `BidMatrixVersion` | both are "snapshot/versioning" semantics; the boundary cannot be read from the names | clearly designate one as alignment versioning, one as matrix snapshot, and fix it in the docs |
| Tender\* vs Bid\* prefix | implies a "tender side (procurement list) vs bid side (quotes)" split, but it is undocumented; `BidInvitation` (tender side issues the invitation) carries the Bid prefix, violating this implied convention | write the bounded contexts explicitly into the docs; review the placement of `BidInvitation` |

### 10.3 Business-service naming (name does not match reality)

(corrected 2026-06-23: `bid_matrix.py` is now ~1291 lines but the `bid_evaluation.py` / `bid_recommendation.py` / `matrix_cell.py` split is still pending — see TODO.md §3.)

| File | Name claims | Actual responsibility | Disposition |
|---|---|---|---|
| `bid_matrix.py` | matrix | matrix + evaluation `_evaluate_cell` + recommendation gate `_compute_recommendation` + baseline + family normalization | split into `bid_evaluation.py` / `bid_recommendation.py` / `matrix_cell.py` |
| `comparison.py` | comparison | actually historical same-spec baseline / spec-index | rename and move into `services/history/` (e.g. `spec_baseline.py`) |
| `statistics.py` | statistics | dashboard + writes `ref_price_*` + writes `recommended_brands` (master-data write side-effect) | split read-only statistics from master-data writes |
| `bid_alignment.py` | alignment | only AI suggest; actual persistence is scattered across `supplier_fill_llm.py` + `routes/analysis.py` in three places | close out into a single `AlignmentService` |

`scoring.py` and `supplier_recommend.py` score in two places; `canonical`/`standardize`/`enhance`/`category_classify` do material-identity normalization in four places — all should be closed out.

### 10.4 Status unification (highest-ROI naming remediation)

(corrected 2026-06-23: partially addressed — `core/enums.py` now exists and consolidates the `RowType` vocabulary (§11.4) plus named constants for cell status, quality gate, and recommendation; but the full three-axis `lifecycle_state` / `presence` / `evaluability` Enum refactor and value-mapping table remain pending — see TODO.md §3.)

The same three-state concept (good / pending / bad) is written in code as **7 vocabularies, with inconsistent casing**:

| Semantics | Existing spellings (per field) |
|---|---|
| "good / pass" | `ok`(row/eval) · `pass`(checksum) · `normal`(alert) · `safe`(match tier) · `firm`(rec) · `AUTO`(quality) · `confirmed`(lifecycle) |
| "needs review" | `pending`(cell/row/item/lifecycle) · `REVIEW` · `partial`(row) · `conditional`(rec) · `risky_*`(match) · `basis_unconfirmed` / `alignment_pending` / `quantity_source_conflict`(eval) |
| "bad / blocked" | `BLOCKED`(uppercase, quality) · `blocked`(lowercase, rec) · `fail`(checksum) · `red`(alert) · `missing` / `invalid` / `excluded` |

**`BLOCKED` and `blocked` — same word, different casing, different fields — are the most bug-prone.**

The deeper problem: the word `status` is shared by three **orthogonal axes**, so the single value `pending` means six different things in six places (submission lifecycle / group lifecycle / cell existence / row / item action / job):

- **lifecycle axis**: draft → confirmed → superseded → finalized → approved / rejected (session / group / submission / version)
- **existence axis**: quoted / aggregated / pending / excluded / missing (cell)
- **evaluability axis**: ok / quantity_source_conflict / basis_unconfirmed / alignment_pending (eval)

And `BidAlignmentItem` uses `action`(align/pending/exclude) at the persistence layer, the UI-layer `_build_review_cell` derives `cell_status`(quoted/aggregated/pending/excluded/missing), and the draft-layer `supplier_fill_llm` writes `status=` — the same concept with three names and three value sets, hard-mapped across boundaries.

**Unification plan:**

1. **Give the three orthogonal axes non-overlapping names**, with each word belonging to only one axis: `lifecycle_state` / `presence` / `evaluability`. No more falling back to a bare `status`.
2. **Define a centralized `Enum` per axis** (add `core/enums.py`), replacing the repo-wide string literals. Currently only `bid_matrix` has `CELL_*` constants, which is an isolated case.
3. **Converge the three main state words onto the `AUTO / REVIEW / BLOCKED` already in Engineering Charter §3**, unify `ok/pass/normal/safe/firm`, etc.; fix one casing repo-wide.
4. **Merge `action` and `cell_status`**: the persistence layer stores the authoritative presence enum directly, the display layer derives (`aggregated` is a sub-state of `quoted`, distinguished by `agg_total`, and need not be a separate value).

Recommendation: produce a value-mapping table (old value → new enum), distinguishing **pure renames (safe)** from **those touching ORM columns / API contracts (need migration + frontend sync)**, landing them with Batch 2/3 remediation.

### 10.5 Ownership of naming remediation

| Item | Remediation batch | Nature |
|---|---|---|
| `status` three-axis split + `core/enums.py` | Batch 2 (stabilize E2E boundary) | touches ORM columns / API contracts / frontend, needs migration |
| `services/` subpackaging + service split | Batch 2/3 | mostly moves and renames, paired with P1-1 |
| promote `TenderAnchor` to a first-class entity | Batch 3 | data-model change, needs migration |
| rename `Quote` → `HistoricalPrice`, etc. | Batch 3 | large-scope rename, needs migration + contract sync |

## 11. Audit addenda (verified items not listed separately in earlier sections)

> The following items were raised during the audit but did not land as trackable items in §3–§10; they are collected here to keep the remediation list complete. All were verified by `file:line`.

### 11.1 Row-level bbox source evidence is missing throughout (§5 / §14)

`SourceRef.bbox` is **read but never written** across the whole repo: it is only read and serialized in coverage computation (`extraction_draft.py:214,309`) and the gate (`:388`), with no code path ever assigning it; `_raw_items_to_draft_rows` only sets `tile_bbox` during tiling (a page fraction, not a pixel bbox). Consequence: row-level positioning coverage is permanently 0, the quality gate permanently rules REVIEW on `bbox_coverage=0`, **no document can reach PASS on row-level bbox**, and the Engineering Charter §4 production goal of "preserve bbox where available" cannot be met.

- Current state: the §6 review table already records "partly holds (bbox is 0, the tiled path has tile_bbox)", but it is not listed as a remediation item.
- Disposition: Batch 3. Designate bbox as an ongoing product-level goal (can be backfilled downstream), and honestly mark "no row-level pixel positioning" in the quality report; do not claim pixel-level full traceability. Consistent with memory `project_rootcause_layers` (bbox can be backfilled downstream).

### 11.2 Soft foreign keys: domain tables carry FKs as bare `Integer` columns (data integrity)

(done per migration `0004_soft_fk`, dated 2026-06-23 — formal `ForeignKey` constraints were added for the four columns below; corrected 2026-06-23: TODO.md still lists this as pending under §11.2 because it was last updated 2026-06-22.)

The following columns declare a foreign key in a comment but are actually bare `Column(Integer)`, bypassing SQLite `PRAGMA foreign_keys=ON` validation, inconsistent with the `ForeignKey(...)` constraints of the rest of the models:

- `bid_alignment.py:37` `tender_list_session_id` (comment `# FK → tender_list_sessions.id`)
- `bid_alignment.py:69` `BidAlignmentItem.submission_id` (no FK to `bid_submissions`)
- `alignment_finalization.py:19` `project_id`
- `bid_matrix_version.py:19` `project_id`

Disposition: Batch 3, add formal foreign-key constraints and indexes alongside Alembic (P2-1).

### 11.3 Dead code / test-only code inside production modules

- `pipeline.py:525` `_assign_source_ref_from_grids`: docstring notes "Used by tests. Production path now goes through table_recognizer."
- `table_recognizer.py:931` `_correct_page_orientation`: docstring notes "[production path disabled] … kept only for test_orientation_correction.py to reference" (production uses `_detect_chain_orientation`).
- `splitter.py` `PageSplitter` and `aggregator.py` `ResultAggregator`: used only by the legacy `_run_batched` (mock/non-dashscope) path; the OCR main path's single-page handling does not call them (confirm callers repo-wide via grep before retiring).

Disposition: Batch 3 structural governance. Move test-only logic out of production modules or mark it explicitly; retire the legacy batch path after confirming no production calls. **Must not accidentally delete user workspace data.**

### 11.4 `row_type` dual enum (same source as the §10.4 status unification)

(done `bdaa890` per TODO.md — the dual vocabulary was consolidated to `quote_line|section_header|remark|invalid|subtotal|grand_total`, with `header`→`section_header` / `note`→`remark` / `empty`→`invalid`, and `BidQuoteLine.row_type` is now persisted via `normalize_row_type(item.row_type)` at confirm; a single `RowType` vocabulary lives in `core/enums.py` (`RT_*`).)

Row-type classification has two inconsistent value sets:

- `table_parser._classify_row` (`table_parser.py:354` etc.): `quote_line / subtotal / grand_total / header / empty / note`
- `table_recognizer._raw_items_to_draft_rows` (`table_recognizer.py:1241,1272`): `quote_line / subtotal / grand_total / section_header / remark / invalid`

`header≠section_header`, `note≠remark`, `empty≠invalid` are the same concepts under different names. Disposition: fold into the §10.4 presence/row-type enum unification, define a single `RowType` Enum. Note also: this `row_type` is currently **not persisted to `BidQuoteLine`** (see P1-3), and downstream falls back to the name regex `合计|小计` — enum unification and field persistence must be handled together.

### 11.5 Cross-cutting duplicate-code list (concrete targets to realize P1-1)

(corrected 2026-06-23: partially done — `parse_id_csv()`, `get_finalization_snapshot()`, and `get_current_confirmed_session()` were extracted; a centralized `services/llm_provider.py` now exists (currently untracked) but the three inline `OpenAI()` call sites are not all wired to it yet; the aggregate-price helper and the markdown-fence parser remain duplicated — see TODO.md §3.)

P1-1 set the direction; the following are concrete duplication points that can be eliminated directly:

- comma-separated integer parsing + `400` exception: copied roughly 7 times across `analysis.py` and `export.py` (the export sites lack try/except, an inconsistent basis) → extract a `parse_id_csv()` dependency.
- the `OpenAI` client is instantiated separately in `/bid-insight`, `/bid-alignment/suggest`, and `/llm-fill` → extract an LLM provider.
- the aggregate-price rule `round(agg_total/agg_qty,4) else unit_price×qty` is copied as a closure in 5 places (within `bid_matrix.py`) → extract a single helper.
- stripping markdown fence blocks from the LLM response: byte-for-byte duplicated in `bid_insight.py` and `bid_alignment.py` → extract a shared parser.
- finalization-snapshot queries in 3 places, current-session query basis inconsistent (see P1-5) → extract `get_finalization_snapshot()` / `get_current_confirmed_session()`.

Disposition: eliminate together with the Batch 2 service close-out.

### 11.6 Coverage cross-check

| Audit-source item | Landing point |
|---|---|
| `first_qt` NameError | P0-1 / §9.1 |
| EvaluationPolicy disguises a known policy | P0-2 |
| candidate recall + recommended_brands pollution | P0-3 / §9.1 |
| 200-page silent truncation | P0-4 |
| production prompt real samples | P0-5 (grading TBD) |
| route/service overload, export duplication | P1-1 / §10.1–10.3 / §11.5 |
| submission-identity migration not closed out | P1-2 / §10.2 |
| recognition audit fields weakly typed, row_type/corrections not persisted, BQL no updated_at | P1-3 / §11.4 |
| OperationLog insufficient for field-level audit | P1-4 |
| current-session basis inconsistent | P1-5 / §11.5 |
| business thresholds scattered | P1-6 |
| no versioned migrations | P2-1 |
| family/DN normalization duplication | P2-2 / §10.3 |
| match writes `Material.extended_attrs` | P2-3 |
| prior-round wrong conclusions retracted | §9.2 |
| directory/entity/service naming | §10.1–10.3 |
| status multi-vocabulary / `status` three axes / `pending` six meanings | §10.4 |
| row-level bbox missing | §11.1 |
| soft FK bare Integer | §11.2 |
| dead/test-only code | §11.3 |
| cross-cutting duplicate code | §11.5 |
