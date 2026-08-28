# MEMPAS — Technical Specification

> Consolidated 2026-08-27 from 40 numbered `docs/design/*.md` documents plus
> `TODO.md` and `HANDOFF.md`. Each statement below is what a design
> document's own status banner marked **current truth** at consolidation
> time. The originals are preserved under `archive/design/` with full
> rationale, measurements, and retraction history — a bracketed tag like
> `[design/40]` points there. This file is the current-state architecture
> reference; it does not re-derive *why* — read the archived original for
> that.
>
> For product/business behavior, see `docs/spec/FUNCTIONAL.md`. Repo layout,
> dev commands, and cross-cutting invariants are in `CLAUDE.md` — not
> repeated here.

---

## 1. Stack

FastAPI ≥0.115, SQLAlchemy ≥2.0, SQLite (WAL), Pydantic ≥2.10, pandas/numpy,
`uv`; Vue 3 ≥3.5, Vite ≥8, Ant Design Vue 4.x, Pinia, Axios
`[design/07-technical-design-v2]`.

---

## 2. Data model & write path

**Staging never pollutes master data.** Uploading and staging supplier PDFs
writes `BidSubmission` + `BidQuoteLine` rows only — `suppliers` / `materials`
/ `quotes` (historical price) are never touched by the bid-comparison flow.
Archiving to historical price is a separate, explicit,
idempotent-by-`archived_quote_id` action: `POST /api/quotes/archive-prices`
`[design/09]`.

- `bid_submissions.supplier_id` is nullable (weak association) — a real
  supplier is required only at archive time (422 if NULL then).
- `bid_quote_lines.material_id` is nullable; a `canonical` JSON column
  carries soft-aligned identity without requiring `material_id`.
  `row_type VARCHAR(32)` (migration `0003`) uses a consolidated vocabulary:
  `quote_line|section_header|remark|invalid|subtotal|grand_total`.
- `supplier_aliases` — `UNIQUE(supplier_id, normalized_alias, alias_type)`.
- Supplier resolution (`services/supplier/supplier_resolve.py::resolve_supplier`)
  is a 7-layer cascade: `Supplier.name` exact → `Supplier.short_name` exact →
  4 `SupplierAlias` types → difflib fuzzy ≥0.85 (candidates only, never
  auto-binds).
- Shared valid-quote filter: `valid_quote_filters()` /
  `valid_quote_query(db)` (`services/history/quote_filters.py`) — excludes
  `bid_status IN ('polluted','excluded_from_ref')` and requires
  `Supplier.merge_status=='active'`. **Known gap**: the dashboard
  heatmap/bubble queries in `statistics.py` do not apply this filter (only
  filter on `bid_status != "未中标"`) — inconsistent with the unified-filter
  rule `[design/11]`.
- `suppliers` carries `merge_status` (active/merged/inactive) +
  `merged_into_supplier_id`.

**Multi-round quoting** (migrations `0009_quote_rounds`, `0010_anchor_uid`)
`[design/42]`:
- `QuoteRound(project_id, category, seq, name, stage, status,
  is_final_basis, tender_list_session_id, confirmed_supplier_ids,
  used_submission_ids, created_by, opened_at, closed_at, remark)`;
  `BidSubmission.round_id` nullable, indexed.
- `anchor_uid` — a stable per-procurement-row identifier assigned at list
  confirmation, carried across list revisions by seq+name/spec identity
  matching; retired rows stay visible in rounds that used them.
- **Corrected data model** (this superseded the original plan mid-build):
  one `BidAlignmentGroup` per `(round_id, anchor_uid)`, not one group shared
  across rounds — because `import_and_match`'s wipe-and-rebuild is currently
  scoped only to `(project_id, category)` with no round awareness. **This is
  not yet fixed**: running match for round 2 today still destroys round 1's
  groups. The fix (round-scoping the rebuild, adding
  `round_id`+`anchor_uid` to `BidAlignmentGroup`, re-pointing
  `record_submission_scope` and the `used_submission_ids` gates onto
  `QuoteRound`) is scheduled but unbuilt ("P2"/"B0").
- Trend computation is planned as a new read-only service `round_trend.py`
  consuming `BidMatrixService` output per round — not built.

**Closed-roster invitation** (design intent — verify shipped status)
`[design/18]`:
- Draft extraction fields: `invited_suppliers: [{raw_name, source_ref}]`
  (literal text, no guessing) and `price_basis: {tax_included, tax_rate,
  currency}` (tri-state true/false/null, never defaulted).
- `ProcurementCase(roster_mode: open|closed|exception)`,
  `BidInvitation(tender_id, supplier_id NOT NULL, raw_name, source, status)`.
- Closed-set matching cascade: user selection → cover-OCR name resolved
  within roster → filename alias; 0 or >1 candidates force manual review.
- Migration `0007` (idempotent): `bid_invitations` +`raw_name`/`source`;
  `procurement_cases` +`roster_mode`; `tender_documents` +`price_basis`.

---

## 3. Recognition pipeline

### 3.1 Shared skeleton

`apps/api/intelligence/extraction_draft.py` / `table_recognizer.py` — one
shared skeleton (`recognize_tables`) drives both the tender/procurement-list
side and the quote side via adapters (TenderAdapter, QuoteAdapter). Shared
dataclasses: `SourceRef` (incl. `tile_bbox`), `DraftRow` (`field_sources`,
`extra_fields`, `parser_mode`), `ExtractionDraft` (`review_candidates` —
isolated from `rows`), `QualityReport` (page accounting, arithmetic-mismatch
gate, `failed_target_pages`) `[design/10]`.

Centralized thresholds (`domain_config.py`, bottom of `extraction_draft.py`):
`_EXPECTED_ROWS_MIN_RATIO=0.70`, `_DECLARED_TOTAL_DIFF_BLOCKED=500.0`,
`_DECLARED_TOTAL_DIFF_REVIEW=50.0`, `_REVIEW_PAGE_RATIO=0.30`.

Retry-exhaustion: >30% of target pages in REVIEW → whole document BLOCKED;
no pseudo-complete filling, ever.

### 3.2 Engine routing (current, three-way)

`[design/25][design/26]`

1. **Native PDF with a usable text layer** → deterministic text extraction
   (`intelligence/tender_text_layer.py`), no vision call. ~20–25× faster
   than the vision path (measured ~14–18s vs ~364s on the reference doc).
   Falls back to (2) automatically (logged, not silent) if no usable text
   layer or the anchor table doesn't parse cleanly.
2. **Scanned PDF** → PaddleOCR-VL (`parser_mode`/`input_mode` =
   `"paddle_vl"`), the sole visual engine for both tender and bid documents.
   Adapter serializes Paddle's `cells` matrix to canonical CSV and feeds the
   existing `build_draft()` — same gates/tiers/ledger as before, zero drift
   between what's measured and what's enforced in production.
3. **BLOCKED / untrusted** → honest dead-stop, no silent second-engine
   fallback (current default; see §7 "qwen retention" for the suspended
   decision to add a third pixel-reread tier).

New modules: `paddle_tender.py` (Paddle → tender-CSV →
`vl_tender.build_tender_draft()`), `paddle_doc_meta.py` (doc-type-agnostic
cover-scalar + declared-requirements extraction from Paddle's own OCR
**text**, via a text-only LLM call — not vision).

`.claude/rules/recognition.md`'s ban on "same-document mixed extraction"
does **not** apply to the deterministic text-layer vs. vision **document-level**
choice (§3.2.1 vs 3.2.2) — that's an allowed document-level either/or, always
labeled honestly via `parser_mode`. It also doesn't apply to the gap-fill
carve-out (§3.4) — see that section for the four binding conditions.

**Not yet done**: Track A's own brand/requirement-text extraction for
native-text-layer tenders still calls qwen vision, not yet rewired to the
text-only `paddle_doc_meta` approach; no real-API acceptance run for the
new tender adapters (only offline-validated); qwen-vl-ocr was evaluated as a
possible Paddle replacement on a single one-page/5-row sample (5/5 unit
price correct, 4/5 total price, one hallucinated value, a real header/data
column-count mismatch) — **not pursued further**; recommendation is not to
pursue engine replacement without a stronger reason than cost
`[design/41]`.

### 3.3 Page classification

Production page classification uses **visual** models on rendered
thumbnails: `qwen3-vl-flash` (flash pass) + `qwen3-vl-plus` (review pass),
run on every page (`table_recognizer._classify_pages`,
`dashscope_ocr.classify_pages_visual`/`review_pages_visual`). A rule-on-HTML
/ text-LLM classifier survives only as a coarse fallback
(`page_classifier.classify_page`) `[design/04]`.

**Page-filter cost-reduction** (`intelligence/page_filter.py` +
`pipeline.py`) — classifies pages and sends only quote pages to Paddle.
**Default off**, no-op unless `MIMO_API_KEY` is set. Three test-pinned
defenses: (1) multi-round union voting (`FILTER_ROUNDS=2` — single-round
classification has measured real run-to-run false negatives); (2) "when in
doubt, send" (any failure/ambiguity counts as send, never as skip); (3)
closed accounting ledger — `total == sent + skipped` must reconcile per
page, else the whole document is sent. Concurrency fix (windows and rounds
parallelized, previously serial for no reason) took Taikelong 53-page
classify from minutes → 136.7s → 58.7s. Net tradeoff: 79% Paddle-cost
reduction but 33% slower end-to-end — a product decision not yet made
`[design/41 update / HANDOFF]`.

### 3.4 Gap-fill

`apps/api/intelligence/gap_fill.py`. Four binding conditions (the carve-out
from the "no mixed extraction" rule): (1) only `AMOUNT_EMPTY` cells, never
overwrite a present value; (2) `field_sources[field]="llm"`, never
impersonates `direct_cell`; (3) the filled row must pass an arithmetic
identity check (`qty×unit_price≈total`, `total×rate≈tax`,
`total×(1+rate)≈incl`, tolerance `EXTRACTION_ARITHMETIC_TOLERANCE=0.03`) —
fails → discarded, not stored; (4) quality tier never rises from a fill
`[design/33]`.

- Orientation: lazy fan-out over angles `(0, 90, 270)` in order, stop at
  first pass; a `DataInspectionFailed` refusal at one angle is a normal
  outcome (moves to the next), not an error.
- Vendor: `get_production_filler()` follows the **visual** switch
  (`VISION_CLIENT_VENDOR`), not the text switch — a dedicated test
  (`test_gap_fill_follows_the_vision_switch_not_the_text_one`) pins this,
  because gap-fill directly affects amounts.
- Same-orientation Paddle-only and upright-rotated Paddle-only alternatives
  were both tested and rejected (identical blanks / unusable structure) —
  a second model call remains structurally required.
- Measured payoff: on one document, 9 gap rows recovered = 100% of that
  doc's −26.22% total deviation; ~96%/~67% of two other documents' gaps
  similarly explained.
- **Known gap**: `field_sources`/`gap_filled` is computed but discarded
  before reaching `job.result` — the frontend `QuoteGrid` doesn't render
  filled cells as visually distinct despite the spec requiring it.

### 3.5 Column-shift detection

`_dirty_amount_slots` (general, type-based, no document-specific constants):
flags a row `column_shift` when a slot that must hold a number instead holds
free text that's neither numeric nor a "not quoted" marker (`/`, `无`)
`[design/34]`.

- Per-column recovery, not per-row: refuses `qty`/`unit_price`/`tax` (near
  the drop point, contaminated by neighbor-row smearing) but recovers
  `total` under three required conditions: shift is exactly 1, exactly one
  dirty slot, source cell differs from the previous row's same-position
  cell. Two exceptions let money fields survive if the row's own arithmetic
  still closes.
- Composes with gap-fill: a refused cell becomes `AMOUNT_EMPTY`, i.e.
  becomes gap-fill's addressable population.
- Submission-level block: `INTEGRITY_COLUMN_SHIFT_BLOCKED_RATIO`=2%,
  `INTEGRITY_COLUMN_SHIFT_BLOCKED_COUNT`=3 — **flagged as possibly
  miscalibrated** for this detector (inherited from an older, different
  detector), not re-validated.
- **Structural bias, documented not fixed**: the arithmetic-based "how much
  to blank" guard never recovers money on tables without a tax column
  (0/69 measured on two documents), because `qty` is usually the dirty slot
  there — degenerates to "blank everything." No held-out document exists;
  all rules were derived and validated on the same 7 fixtures that test
  them.

### 3.6 Cross-row misalignment (design, not built)

A distinct defect class from column-shift: an entire row's numeric block
(qty/unit_price/total) is offset one row against the correct name/spec,
while each row stays internally arithmetic-consistent — so no within-row
check can catch it. Proposed (unbuilt) detector: cross-supplier quantity
voting — a row whose quantity disagrees with ≥2 agreeing peers is flagged.
Measured to catch 16/19 known cases (misses a separate dropped-digit
class). This check would need to run at alignment/preview time (needs the
full peer set), not single-document recognition `[design/37]`.

### 3.7 Column → role resolution ("table-first pipeline", L1 — built)

`apps/api/intelligence/column_roles.py`: shared role vocabulary
(`ROLE_LABELS`) + `verify_roles` (arithmetic/type verification gate) + two
LLM proposers (`propose_by_llm`, `propose_layout_by_llm`), wired as
keyword → verify → model-only-on-failure → verify, into
`tabular_ingestion.resolve_columns` (quote side) and
`tender_list._layout_by_llm` (anchor side) `[design/40]`.

- **Core rule, load-bearing**: column→role mapping may use a model (it's
  answering a question about *schema*, verifiable by arithmetic before
  storage); row→row alignment must never use a model (quantity sequences
  are already deterministic; a commutative swap like qty↔unit_price is
  structurally unfalsifiable by a single-document arithmetic check — this is
  what `.claude/rules/recognition.md` and CLAUDE.md §4 mean by "LLM must not
  re-rank candidates").
- The model may only fill roles the keyword table left empty
  (`_only_missing`/`_merge_proposal`) — it can never overwrite a
  keyword-identified role.
- **Retracted findings** (do not treat as true): the original claim that a
  CSV round-trip was a major accuracy loss source, and that removing it
  ("L0", proposed, unbuilt) would be the single largest accuracy fix — both
  measured false. The real amount-loss driver was rows with zero returned
  amount cells at all, fixed by gap-fill (§3.4), not by removing the CSV
  round-trip.
- Live per-document model-call cost (measured): Tier 0 classify on
  Excel/CSV = 0 calls; on scanned PDF = 1 vision call; quote PDF = 1 Paddle
  submission + 1 text call; text-layer tender = 0 (aside from the
  not-yet-migrated brand/requirement extraction); semantic-matching
  fallback ≈72 embedding calls per typical project — the most expensive
  step, now largely avoidable given deterministic alignment (§3.8) applies
  more often.

### 3.8 Alignment

**Anchor-mode alignment** (with a confirmed procurement list) — the
authoritative architecture `[design/05]`: canonical-key normalization (the
LLM's only job) feeds a matching engine shared by anchor mode and fallback
mode. Gate① automated validation is live; Gate② LLM adversarial review and
the canonical-mapping cache are designed, not implemented.

**Deterministic subsequence alignment** (`_subsequence_positions`) — a
supplier's quote-row order must be an order-preserving subsequence of the
anchor row order by quantity: left-greedy and right-greedy quantity
matching agree where they can; where they disagree, candidates in the
feasible window are scored by text similarity requiring a lead margin
(`SUBSEQ_TIEBREAK_MARGIN`); any undecidable row aborts the whole submission
back to semantic-matching fallback. Text similarity only ranks
quantity-admitted candidates, never independently creates a match.
Guards: `_chance_agreement` rejects degenerate all-quantity-1 cases;
anchors with quantity 0/blank must stay under `SUBSEQ_MAX_WILDCARD_RATE`
(10%) `[design/39]`.

**Quote-derived (anchorless) alignment** — when no confirmed procurement
list exists: reference submission = the one with the most item rows (ties →
lowest `submission_id`); other submissions align to it positionally via the
**existing, unchanged** `_sequential_matches`/`import_and_match`. This is
enforced at the schema layer, not by service code remembering to behave:
`BidMatrixResult._quote_derived_axis_is_preview_only` forbids pairing
`axis_kind='quote_derived'` with anything but `basis='preview'`
`[design/32]`.

- Item-row rule (`A1`, shared, `ingestion/list_rows.py`): a row counts as an
  item iff its quantity cell parses as a number, **and** for detecting
  non-item rows: no quantity **and** either all of name/spec/unit are
  identical+non-empty (label bled across columns), or text matches
  `FOOTER_MARKERS`. Two naive alternatives (bare "no quantity → drop";
  "amount equals sum of other rows" self-check) were tested and rejected —
  both silently deleted or missed real rows.
- `confirm_batch` gained a `gates_advisory` switch, independent of
  `dry_run`: `dry_run` controls persistence, `gates_advisory` controls
  whether a gate failure aborts. Preview = sandbox write (rolled back) +
  advisory gates; official path keeps both strict.

**Block-level alignment** (`services/block_alignment.py`) — two-level:
block correspondence via quantity-sequence key (LLM only when the
deterministic key can't decide, order-preserving fallback otherwise), then
in-block order-preserving alignment; conflicting rows go to `pending`
individually, don't poison the table. **Caution**: the accuracy figures from
the original tuning round are self-labeled as offline-prototype data on a
since-abandoned qwen3.7-plus pipeline — only the *methodology*
(quantity-sequence block keying, no-golden self-check via a document total
row) is durable; the numbers are not `[HANDOFF, topically owned by design/32]`.
Not yet done: wiring `block_alignment` into `anchor_match`'s production path
(current sequential-direct-connect requires `dn_cov ≥ 0.90`, a valve-specific
threshold that doesn't generalize to e.g. cable categories).

### 3.9 Integrity gates (`draft_integrity.py`)

Four ingestion gates: column-shift detection, duplicate-row detection,
arithmetic-closure check, truncation check. `row_identities_hold()` and
`check_sequence_continuity()` (row-conservation, `SEQ_COVERAGE_MIN=80%`
coverage or verdict downgrades to `not_applicable` → REVIEW) live here and
are reused by gap-fill's own arithmetic gate (§3.4) — same yardstick, not
two independently-drifting implementations.

### 3.10 Checksum gate (declared total vs. sum of detail lines)

Current live threshold: `CHECKSUM_BLOCK_DELTA_RATIO = 0.5%` relative
(`domain_config.py`, consumed by `_gate_integrity` in
`quote_confirmation_service.py`). A `checksum_ack` path lets a reviewer
explicitly acknowledge and proceed past a discrepancy, recorded.
`pass`/`fail`/`unknown` (missing declared total ≠ pass) — three-state, kept
distinct `[design/20]`.

- **Not implemented, proposed only**: replacing the ratio threshold with a
  per-row absolute tolerance (`row_count × PER_ROW_ROUNDING_TOLERANCE`,
  proposed 0.01¥/row) — motivated by an audit finding the 0.5% ratio caught
  only 1 of 4 documents with real defects, with a measured clean-extraction
  noise floor ~25,000× below the current threshold.
- Retracted: the earlier rationale that 0.5% was validated by "catching a
  0.63% case" — that only established a sensitivity floor, never a correct
  ceiling.

### 3.11 Robustness backlog (proposed L0–L4, only fragments built)

`[design/19]` — landed: row-conservation ledger via
`check_sequence_continuity` (§3.9); `_build_checksum` instrumentation
(line_count/abs_delta/delta_pct/per_row_delta logged on every confirm).
**Not built**: multi-sheet composite anchor key `(sheet_ordinal, seq)` with
continuation-row handling; four-way orientation detection (0/90/180/270,
current classifier never tries 180°); per-page provenance ledger (rotation
applied, classifier-vs-probe origin, empty-yield reason). An image
ink-projection-profile axis heuristic was tested and rejected (table ruling
lines dominate the signal, doesn't separate axes reliably) — recorded so
it's not re-attempted.

---

## 4. Vendor / provider switches

Two independent switches in `domain_config.py`, **not** scattered across
call sites `[HANDOFF, .claude/rules/recognition.md]`:

- `TEXT_CLIENT_VENDOR` (`'dashscope'` | `'mimo'`)
- `VISION_CLIENT_VENDOR` (`'dashscope'` | `'mimo'`)

Deliberately separate — the two call classes have different failure
consequences and different verification methods, so switching them together
would force accepting both risks at once.

- Text-vendor client comes from exactly one place:
  `services/llm_provider.get_text_client()` — no call site may hardcode a
  model name (this was previously violated twice: `bid_insight` hardcoded
  `"qwen-plus"`, `block_alignment`'s default parameter hardcoded it again).
- Vision-vendor mimo path is `providers/mimo_vision.MimoVisionProvider`
  (OpenAI-compatible `chat.completions`, structurally different protocol
  from dashscope's private SDK — not just a base_url swap).
- Configured `mimo` with no `MIMO_API_KEY` → explicit, logged fallback to
  dashscope, never silent (test-pinned both sides).
- **Embedding cannot migrate** — mimo has no embedding API;
  `anchor_match._embed` is hard-locked to dashscope. This is documented as a
  hard constraint (asserted by a test that the docstring says so), not an
  oversight.

**Nine total qwen/dashscope dependencies identified**, 8 migrated to the
mimo switch: cover-page scalars, card summary, `bid_insight`, material
`enhance`, `block_alignment`, scanned bid/tender classification, gap-fill
(§3.4), tender VL-direct fallback. Real-call-tested for wiring; full
output-quality verification exists for cover-page scalars, scanned-doc
classification, and gap-fill specifically — `bid_insight`/`enhance`/
`block_alignment` output quality is unverified beyond wiring. Full backend
suite at time of migration: 1136 passed / 18 skipped / 0 failed.

---

## 5. Domain services (backend architecture)

Seven authoritative services extracted `[design/12]`: `TenderSessionService`,
`SubmissionScopeService` (`bid_submission_resolve`), `QuoteConfirmationService`,
`AlignmentService`, `BidMatrixService`, `EvaluationPolicyService`,
`BidExportService`.

`EvaluationPolicy`: `get_evaluation_policy(project_id)` currently returns
`UNKNOWN_EVALUATION_POLICY` (method/award_mode unknown,
`final_decision_requires_committee=True`) for every project — deliberately,
rather than fabricating a policy — until tender-document policy persistence
exists.

Typed domain errors: `apps/api/core/errors.py` —
`DomainError`/`ValidationError`/`NotFoundError`/`ConflictError`/
`ReviewRequiredError` + `register_exception_handlers`; services raise these,
not `fastapi.HTTPException` directly `[design/22]`.

Column-identity fix (B3, resolved): the mislabeled `supplier_id` key
(actually holding a column id) in `SupplierCell`/`MatrixTotal`/eval
structures was renamed to a generic `id`/`ids`; `submission_id` is the real
modern identity field (null in legacy mode). All join sites now use
`submission_id ?? id`, never `supplier_id` `[design/22]`.

Audit events: `services/audit.py::write_domain_event()` (no auto-commit —
caller commits in the same transaction as the business write) — single
JSON `payload` column on `OperationLog` (`{event_type, identity, before,
after, meta}`), 7 instrumented event types: `bql_confirm`,
`tender_session_confirm`, `alignment_group_confirm`, `alignment_item_confirm`,
`alignment_bulk_confirm`, `alignment_finalize`, `llm_fill_persist`
`[design/14]`.

**Known, not-yet-done structural debt** (still real, per `TODO.md` §0–§2):

- `routes/analysis.py` is ~2470 lines / 31 routes across 5 concerns — a
  pure file split (Plan A, no URL change) is designed but not executed; a
  URL-prefix rename (Plan B) is deliberately deferred as accepted debt
  (breaking change, needs front+back coordination).
- Two "fat" routes (`tender_list_match`, `tender_list_llm_fill`) still hold
  real business logic in the route layer rather than a service — proposed
  extraction (`MatchOrchestrationService`) not started; must not be folded
  into a mechanical file-split, needs its own design-first pass.
- Domain-normalization logic (valve family/DN) is duplicated across the
  canonical service, `anchor_match`'s coarse classifier, and prompt rules —
  a unifying `MaterialIdentityService` is proposed, not built.
- The "offline" test suite has no network guardrail — a real DashScope call
  once leaked through an unmocked test; a socket-level interceptor or an
  explicit zero-external-call fixture assertion is proposed, not built.
- Row-level `SourceRef.bbox` is write-never across the whole repo (0%
  coverage) — declared a **permanent, ongoing product goal**
  (backfillable downstream), not a bug; quality reports must say "no
  row-level pixel positioning," never claim full pixel traceability.

---

## 6. Auth & roles (backend)

No new tables — role enforced via a reusable FastAPI dependency reading the
role string out of the JWT payload; `users.role` already existed as
`String(16)` `[design/16]`.

- `core/security.py`: `hash_password`/`verify_password` (PBKDF2-HMAC-SHA256,
  260k iterations), `create_access_token`/`decode_access_token` (JWT HS256),
  `get_current_user`, `require_role(*roles)`, `require_admin`.
- JWT payload: `{"sub", "role", "user_id", "exp"}`.
- All non-auth routers already carry app-level `Depends(get_current_user)`;
  admin-only endpoints add `require_admin`.

---

## 7. Migrations

Plan B (incremental, coexists with `create_all`) — not Plan A (Alembic-only
authority), which remains an undone future option `[design/13]`.

- Startup order: `create_all` → frozen `_ensure_sqlite_schema` → stamp
  `0001_baseline` if unversioned → `alembic upgrade head`.
- `0001_baseline` is a no-op anchor revision.
- `_run_alembic_upgrade()` builds the Alembic `Config` programmatically
  (not via `alembic.ini`'s `sqlalchemy.url`, which chokes on non-ASCII
  Windows paths) — injects the app engine's live connection.
- All idempotency guards use `sa.inspect`-based existence checks
  (`_has_column`); one narrow exception (`0004_soft_fk._fk_exists`) wraps a
  presence check, not a DDL-error swallow.
- Revisions shipped: `0002_bql_updated_at`, `0003_audit_fields`,
  `0004_soft_fk`, `0007_anchor_missing_ack`, `0009_quote_rounds`,
  `0010_anchor_uid`.

---

## 8. Open technical decisions

- **qwen retention** (suspended, pending user call informed by real
  BLOCKED-rate evidence) `[TODO §5]`. Two candidate shapes:
  - **A** — delete qwen entirely; text layer → Paddle → BLOCKED is an honest
    terminus (doubt inbox + Excel/re-scan human recourse).
  - **B** — keep qwen as a third-tier pixel-reread fallback: text layer →
    Paddle → qwen pixel reread → still-BLOCKED terminus. **If B is chosen,
    three constraints are mandatory** (their absence previously caused
    real, documented harm — an LLM text review once silently rewrote a
    correct declared total to a wrong one, permanently hiding a real gap):
    1. Triggered only by a deterministic gate's verdict (BLOCKED / large
       key-field loss) — never "let the LLM look and see if it agrees."
    2. Input must be the **original image**, never Paddle's already-extracted
       CSV handed over for "correction."
    3. Must carry a `parser_mode` label, document-level either/or, never
       silent.
- **Page-filter cost/speed tradeoff** (79% cheaper, 33% slower
  end-to-end) — not yet decided, currently ships default-off `[design/41]`.
- **Column-shift recovery thresholds** (2%/3-row) — inherited from an older
  detector, flagged as possibly too strict for the current type-based
  detector, not recalibrated.
- **Preview-lane confirm buttons** — proposal to remove them entirely
  (structurally can't work under the rollback sandbox) plus a real
  「校对入库」 action routing to the supplier tab — designed, not built
  `[design/36]`.
