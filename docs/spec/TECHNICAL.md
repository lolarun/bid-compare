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
>
> **Calibration pass 2026-08-28.** The 2026-08-27 consolidation trusted each
> design doc's own status banner without re-reading the code, and several
> banners were stale. §2 (round scoping, trend service, closed roster,
> statistics filter), §3.3, §3.4, §4, §5, §7, and §8 have since been
> re-verified against the tree and corrected in place. Claims elsewhere in
> this file have **not** been re-verified — grep before relying on any
> "not built" statement.
>
> Every correction below is stated as what the code does today, with the
> superseded claim named, so nobody re-plans work that already shipped.

---

## 1. Stack

FastAPI ≥0.115, SQLAlchemy ≥2.0, SQLite (WAL), Pydantic ≥2.10, pandas/numpy,
`uv`; Vue 3 ≥3.5, Vite ≥8, Ant Design Vue 4.x, Pinia, Axios
`[design/07-technical-design-v2]`.

**Quality gates (added 2026-09-02).** Both CI (`.github/workflows/deploy.yml`)
and the local `.githooks/pre-commit` now run **ruff** on `apps/` + `scripts/`;
CI additionally runs **mypy**. Before this the backend had no lint and no type
check at all, while the frontend had been gated by `vue-tsc` in both places —
the asymmetry was the reason, not a new-tool preference.

- ruff's ruleset is deliberately narrow (`E9,F,I,UP`, minus `UP042`). ~147
  further findings (`B008` FastAPI `Depends()`, `E402`, `E702`, `B905`, …) are
  listed with counts and reasons in `pyproject.toml` as **deferred, not
  unnoticed**. Adoption fixed 482 findings automatically plus 17 by hand;
  those 17 included 7 annotations naming types the module never imported
  (`OpenAI`, `ExtractionDraft` — harmless at runtime under
  `from __future__ import annotations`, but unresolvable for any checker) and
  6 dead assignments.
- mypy runs at **default leniency**, via `uv run` so it can see the real
  third-party types. It is gated against the 115 modules that are already
  clean; the 46 that still carry 167 pre-existing errors are listed under
  `[[tool.mypy.overrides]]` as a debt list that may only shrink.

---

## 2. Data model & write path

**Declaration style: SQLAlchemy 2.0 typed declarative (migrated 2026-09-02).**
All 297 columns across the 21 model classes moved from `x = Column(...)` to
`x: Mapped[T] = mapped_column(...)`; `Base` was already `DeclarativeBase` and the
query layer already 2.0-only (zero `Session.query()` in `apps/api/`, ~229
`select()` sites). Only the declarations lagged, and that lag is what kept the
entire ORM surface typed as `Column[Any]` and therefore invisible to any checker.

- **Nullability came from `Base.metadata`, never from the source text.** Plain
  `Column(String)` defaults to `nullable=True`, whereas `mapped_column` derives
  nullability from the *annotation* — so a literal transcription would have
  silently flipped columns to `NOT NULL`. Existing databases would not show it
  (`create_all` skips existing tables); a freshly created one — which is what
  every test uses — would start rejecting inserts.
- The guard was a full `CreateTable`/`CreateIndex` dump taken before and after:
  **21 tables / 461 lines, byte-identical**. Any future bulk change to the model
  layer should be held to the same check rather than to a code review of 297
  diff hunks.
- `JSON` columns are annotated `Mapped[Any]`, not `Mapped[dict]`: several hold
  lists (`recommended_brands`, `confirmed_supplier_ids`). Since `Any` cannot
  carry nullability, those columns get an explicit `nullable=`.
- The 17 `relationship()` declarations were **left untyped** in this pass.

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
  `Supplier.merge_status=='active'`. **Known gap, still open (re-checked
  2026-08-28)**: `services/history/statistics.py` applies
  `valid_quote_filters()` in 8 of its 10 query sites, but
  `get_dashboard_heatmap` and `get_dashboard_bubble` still filter only on
  `Quote.unit_price > 0, Quote.bid_status != "未中标"`. Excluded/polluted
  quotes and merged-away suppliers therefore reach the dashboard's two
  charts — a direct violation of CLAUDE.md §4's isolation invariant, in the
  one surface where nobody would think to check `[design/11]`.
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
  across rounds. **Built** (migration `0011_alignment_round_scope`, commit
  `57cf535`) — the 2026-08-27 consolidation said "not yet fixed", which was
  already wrong at consolidation time. `import_and_match(round_id=...)`
  appends `BidAlignmentGroup.round_id == round_id` to its wipe scope
  ([`anchor_match.py:1051`](../../apps/api/services/alignment/anchor_match.py)),
  so re-running match for round 2 no longer deletes round 1's groups. The
  migration backfills every pre-round group onto round 1 of its own
  `(project_id, category)`. `round_id=None` deliberately keeps the old
  scope-wide behavior for callers with no round concept (e.g.
  `preview_service.py`).
- Trend computation: `services/matrix/round_trend.py`
  (`compute_round_trend` / `round_trend_to_dict`) — **built**, exposed as
  `GET /api/analysis/round-trend`, covered by `tests/test_round_trend.py`,
  rendered by `apps/www/src/views/compare/components/RoundTrendPanel.vue`
  (only when ≥2 rounds exist, and never in the preview lane, since preview
  writes no alignment groups). Rounds whose `tender_list_session_id` was
  never recorded are reported in `skipped_rounds` rather than guessed at.

**Closed-roster invitation** — **not built** (verified in code 2026-08-28;
this is the "verify shipped status" the 2026-08-27 consolidation asked for).
`roster_mode` / `invited_suppliers` have zero occurrences in `models/`,
`schemas/`, `routes/`, `services/`, or `intelligence/`, and migration
`0006_procurement_case` is an explicit **tombstone**: its `upgrade()` is a
no-op, kept on disk only because databases are already stamped at it and
deleting the file would make `alembic current` unresolvable. Its docstring
instructs that adoption must add a *new* idempotent revision rather than
resurrect it. On already-stamped databases the `procurement_cases` table and
`procurement_case_id` columns physically remain, unreferenced and inert. The
`BidInvitation` model that exists today is the older recommendation-invitation
feature used by `routes/invite.py`, not design/18's roster. The shape below is
design intent only `[design/18]`:
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

**Corrected 2026-08-28 (three independent verification passes caught this
the same day it was first written — the original 2026-08-27 consolidation
trusted design/10's "implemented" banner without checking the file still
exists):** `table_recognizer.py` and its `recognize_tables()` skeleton /
`TenderAdapter` / `QuoteAdapter` dispatch **do not exist** — physically
deleted 2026-08-11 once it was confirmed the legacy TableGrid path was
unreachable in production (`[design/21]`; commit `c60217f`, "F1+F2+F4
legacy 识别链物理删除"). `grep -r "recognize_tables\|TenderAdapter\|QuoteAdapter"`
across the whole tree returns zero hits.

What survives: the shared dataclasses `SourceRef` (incl. `tile_bbox`),
`DraftRow` (`field_sources`, `extra_fields`, `parser_mode`),
`ExtractionDraft` (`review_candidates` — isolated from `rows`),
`QualityReport` (page accounting, arithmetic-mismatch gate,
`failed_target_pages`) still live in `apps/api/intelligence/extraction_draft.py`
`[design/10]`. But the entry point that fed them is now `paddle_vl.py` /
`paddle_tender.py`, wired through `pipeline.py`, as §3.2 below describes —
not the shared-skeleton architecture this section originally named.

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

**Since fixed** (`[design/21]` §2.2/§2.3 flagged these as "silently inert"
on the VL quote path in 2026-08-11; closed since): `vl_quote.py` now passes
`supplier_name`/`declared_total` through to `build_draft()`
(`vl_quote.py:700-701`), and `document_ingestion.py` populates
`merged["_doc_meta"]` from the pipeline's `doc_meta` output
(`document_ingestion.py:89-90`, `pipeline.py:370`).

### 3.3 Page classification

Production page classification uses **visual** models on rendered
thumbnails: `qwen3-vl-flash` (flash pass) + `qwen3-vl-plus` (review pass),
run on every page (`apps/api/intelligence/providers/dashscope_ocr.py`'s
`classify_pages_visual`/`review_pages_visual` — corrected 2026-08-28: the
call site is not `table_recognizer._classify_pages`, which is dead code per
§3.1, and the module path is `providers/dashscope_ocr.py`, not
`dashscope_ocr.py`). **There is currently no rule-based fallback
classifier** — `page_classifier.py` (the "coarse fallback" `[design/04]`
described) was deleted in the same 2026-08-11 legacy-chain removal as
`table_recognizer.py` (`[design/21]`) and was never recreated; `grep -rn
"classify_page\b" apps/api/` outside tests/`__pycache__` returns nothing.
Page-role classification today is visual-model-only, or a hardcoded
`role="quote_table"` on the VL quote path (§3.2).

**Page-filter cost-reduction** (`intelligence/page_filter.py` +
`pipeline.py`) — classifies pages and sends only quote pages to Paddle.
**Default off** — but "off" is not an independent switch: the classifier is
`None` (hence "send everything", byte-for-byte the pre-feature path) **iff
`MIMO_API_KEY` is unset** (`page_filter.get_production_classifier`). Since
2026-08-27 that same variable is what makes the now-default `mimo` text/vision
vendors work at all (§4), so **setting the key to fix the vendor default also
silently switches this on** — turning an explicitly undecided product tradeoff
(§8) into a side effect of an unrelated deploy fix. **Fixed 2026-08-28**:
`domain_config.PAGE_FILTER_ENABLED` (default `False`) is now a separate,
required condition — the key stays necessary but is no longer sufficient.
Both directions are test-pinned in `test_page_filter.py`
(`test_enabled_flag_is_independent_of_the_mimo_credential`,
`test_credential_is_still_required_when_enabled`). Three test-pinned
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
- **Known gap, confirmed still open 2026-08-28**: `field_sources` /
  `gap_filled` never leave the backend's internals. `field_sources` has zero
  occurrences in `routes/`, `schemas/`, or `services/`; `gap_filled` survives
  only as one `validation_flags` read inside
  `quote_confirmation_service.py:878`. `apps/www/src/components/QuoteGrid.vue`
  references neither, so a model-filled amount is pixel-identical to a
  directly-extracted one on screen — the provenance the four binding
  conditions exist to preserve is discarded at the last hop.

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

**Block-level alignment** (`services/alignment/block_alignment.py` — the
path in the original text was stale) — two-level:
block correspondence via quantity-sequence key (LLM only when the
deterministic key can't decide, order-preserving fallback otherwise), then
in-block order-preserving alignment; conflicting rows go to `pending`
individually, don't poison the table. **Caution**: the accuracy figures from
the original tuning round are self-labeled as offline-prototype data on a
since-abandoned qwen3.7-plus pipeline — only the *methodology*
(quantity-sequence block keying, no-golden self-check via a document total
row) is durable; the numbers are not `[HANDOFF, topically owned by design/32]`.
Not yet done (re-checked 2026-08-28: `block_alignment` has **zero call
sites** in `services/` or `routes/` — it is reachable only from its own
tests, i.e. dead code carrying a live maintenance cost): wiring
`block_alignment` into `anchor_match`'s production path
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
- **Both switches default to `mimo` since 2026-08-27** (`domain_config.py`
  lines 278/284). Two consequences the change did not carry through, both
  open as of 2026-08-28:
  - `MIMO_API_KEY` was missing from `apps/api/.env.example` and
    `docker-compose.prod.yml` outright, and `docs/DEPLOY.md` documented it as
    an *optional* entry that "only enables design/41's page filter" — accurate
    when written, wrong once the default flipped. Production therefore takes
    the dashscope fallback branch on every migrated call site, so the switch
    has no production effect and nothing but a log line says so.
    **Fixed 2026-08-28** in all three artifacts.
  - `services/llm_provider.get_text_client`'s docstring still asserts
    "**默认 `dashscope` = 现状**", contradicting the code it documents.
- Configured `mimo` with no `MIMO_API_KEY` → explicit, logged fallback to
  dashscope, never silent (test-pinned both sides). Note this fallback is
  what makes the missing-key situation above *safe* rather than broken — but
  also what makes it invisible.
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
`BidExportService`. `services/` has since been subpackaged by bounded
context per design/12 §10.1's own recommendation: `alignment/`, `history/`,
`ingestion/`, `matrix/`, `submission/`, `supplier/`, `tender/`.

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
after, meta}`), 8 instrumented event types (corrected 2026-08-28, was
listed as 7): `bql_confirm`, `tender_session_confirm`,
`alignment_group_confirm`, `alignment_item_confirm`, `alignment_bulk_confirm`,
`alignment_finalize`, `llm_fill_persist` `[design/14]`, plus
`anchor_missing_ack` `[design/23]`.

**Project overview aggregation** (`services/tender/project_overview.py`, added
2026-08-30 `[design/45]`) — one read-only service shared by both entry points so
they cannot answer the same question differently:

- `GET /api/projects/overview` (list) gained `include_empty` (default `False`,
  applied **before** pagination so `total` matches `items`) plus per-category
  `has_confirmed_list` / `submission_count` / `next_action`, and a project-level
  `pending_intake_count`.
- `GET /api/projects/{id}/overview` (new) returns project scalars + per-category
  list/rounds/suppliers/next_action in one batched pass. It **computes no
  matrix**: ranking and the three-state gate require `import_and_match`, so the
  overview page lazy-loads `POST /api/analysis/bid-matrix` — the same endpoint
  the matrix page uses (CLAUDE.md §4 "one business result"). A test asserts the
  overview response carries no `recommendation_level` / `evaluated_total` /
  `ranking` / `recommended_supplier` key, so a cheap second opinion cannot grow
  there later.
- Route order matters: `/{project_id}/overview` is declared **before**
  `/{project_id}`.

Two data-semantics facts this work established, both previously undocumented and
both easy to get wrong:

- **`BidSubmission.status` is always `"pending"`.** `confirm_batch` writes
  `status="pending"` at creation (`quote_confirmation_service.py:614`) and never
  updates it, so it is *not* a review marker — using it as one makes every
  project read "awaiting review". The real signal is `ExtractionJob.lifecycle`
  (`active` = recognized, awaiting confirm; `confirmed` = ingested; `removed`).
  Live DB at time of writing: 58 `confirmed` jobs ↔ 58 submissions, 1:1.
- **`pending_intake_count` is project-scoped, not per-category, on purpose.** A
  job carries no reliable category — it either has not finished recognition or
  offers only a guessed `detected_category`. Attributing by a guess makes the
  number change by itself once recognition lands.

**Known, not-yet-done structural debt** (still real, per `TODO.md` §0–§2):

- **Closed-round matrices are recomputed, not frozen** (found 2026-08-30,
  `[design/45]` §2.1). `WorkspaceView.vue`'s round selector comments say it
  loads 「那一轮自己**冻结的**矩阵」, but it calls `POST /bid-matrix` with
  `round_id` — only the `BidAlignmentGroup` set is round-scoped (migration
  `0011`); the matrix itself is rebuilt on read and can drift when the
  procurement list is revised, suppliers are merged, or the price baseline
  changes. `close_round()` (`quote_round_service.py:125`) stores no snapshot,
  and `BidMatrixVersion` — which already has `matrix_json` / `readiness_json` /
  `recommended_supplier` / approval state — has **no `round_id` column** and 0
  rows in the live DB. Tolerable today because nothing presents historical-round
  conclusions as standing facts (design/45 D-2 keeps closed rounds to a quote
  roster for exactly this reason). The fix, if historical conclusions are ever
  wanted, is `BidMatrixVersion.round_id` + a snapshot on close — not a second
  snapshot mechanism.

- `routes/analysis.py` is **2767 lines / 37 routes** across 5 concerns
  (measured 2026-08-28; it was ~2470/31 when the debt was first recorded, so
  it is still growing) — a pure file split (Plan A, no URL change) is
  designed but not executed; a URL-prefix rename (Plan B) is deliberately
  deferred as accepted debt (breaking change, needs front+back
  coordination).
- Two "fat" routes (`tender_list_match`, `tender_list_llm_fill`) still hold
  real business logic in the route layer rather than a service —
  `tender_list_match` alone spans `analysis.py:1135`–`1859`, ~720 lines in a
  single function. Proposed extraction (`MatchOrchestrationService`) not
  started; must not be folded into a mechanical file-split, needs its own
  design-first pass.
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
- **Category generalization gap** (verified 2026-08-28, `[design/08]`):
  `tender_pdf.py::_score_page` still hardcodes valve keywords
  (工作压力/阀体/密封圈/DN\d) for page-location scoring, and
  `extract_valve_canonical` is called unconditionally by both anchor
  builders regardless of category. No `CATEGORY_PROMPT_MAP`/
  `CATEGORY_SCHEMA_MAP`/category-aware dispatch exists. Uploading a
  non-valve category (cable tray, panel, pipe, pump, HVAC diffuser) risks
  near-zero page-location scores or field misalignment — a direct
  contradiction of CLAUDE.md §1's "never trade generality... for a
  single-sample layout" invariant that predates this file.
- **No production LLM/OCR response cache and no LLM-path rate-limit
  protection** (verified 2026-08-28, `archive/design/code-review-e2e-efficiency.md`
  2026-07-10, both still open): every re-upload of an identical document
  re-runs the full model chain — no content-hash cache exists anywhere in
  `apps/api/`. Separately, the LLM supplier-fill path
  (`services/supplier/supplier_fill_llm.py`, `routes/analysis.py:1938`) has
  no 429/rate-limit retry and only uses the first configured API key even
  when `DASHSCOPE_API_KEYS` lists several — unlike the OCR provider path,
  which rotates keys and handles rate limits.
- **Async routes doing blocking work** (`[design/29] §18`): several routes
  under `intake.py`/`analysis.py` were declared `async def` while doing
  synchronous blocking work (file hashing, real vision/LLM calls), which
  stalls the whole event loop under concurrent uploads. Fixed by converting
  to plain `def` so FastAPI dispatches them to the thread pool; pinned by a
  dedicated test asserting the affected routes are not `async def`.

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
- Revisions shipped (full list on disk, `apps/api/migrations/versions/`,
  re-read 2026-08-28 — the earlier list omitted five):
  `0002_bql_updated_at`, `0003_audit_fields`, `0004_soft_fk`,
  `0005_tender_recommendation_snapshot`, `0006_procurement_case` (**tombstone
  no-op**, see §2), `0007_anchor_missing_ack`, `0008_stage_progress`,
  `0009_quote_rounds`, `0010_anchor_uid`, `0011_alignment_round_scope`,
  `0012_project_created_by`, plus the non-numeric
  `9f343f645e1f_brand_tier_approved_canonical`.

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
  end-to-end) — still not decided, still ships default-off `[design/41]`.
  The blocking issue this bullet used to flag — "default-off" being
  implemented as merely "`MIMO_API_KEY` is unset," the same variable that
  gates the vendor default (§4) — was **fixed 2026-08-28**: see §3.3,
  `domain_config.PAGE_FILTER_ENABLED` is now the independent switch. The
  product decision itself is still open; only the code coupling that made
  it un-decidable is resolved.
- **Column-shift recovery thresholds** (2%/3-row) — inherited from an older
  detector, flagged as possibly too strict for the current type-based
  detector, not recalibrated.
- ~~**Preview-lane confirm buttons** — proposal to remove them entirely...
  designed, not built `[design/36]`.~~ **Built 2026-08-28** (commit
  `bd33e4d`, the same commit that last edited this file — the code changed
  after this sentence was written and nobody reconciled the two until this
  pass): the inert buttons were removed from `BidMatrix.vue`; the formal
  lane now shows a 「去复核 →」 link to `/workspace/:id/align`
  (`AnchorReviewMatrix.vue`, which already handled confirm/exclude
  correctly); the preview lane shows only the 待确认 badge with no fake
  action, gated by a new `preview` prop. Still open from `[design/36]` §4: a
  bulk "校对入库" action naming outstanding suppliers, and moving doubt-copy
  phrasing to a backend `user_message` field (the frontend mapping table
  exists — see FUNCTIONAL.md §7 — but backend-authored phrasing does not).
- **Preview-sandbox lock risk under embedding fallback** (`[design/31]` §7,
  never closed, not previously carried into this file): the write-then-
  rollback sandbox measures safe (~0.05s) for pure DB writes, but
  `import_and_match`'s embedding-based alignment fallback
  (`anchor_match._embed_client()`, real HTTP, ~350 texts/batch) can run
  *inside* the sandbox's write transaction — a concurrent real confirm can
  then hit SQLite's lock timeout and fail with "database is locked." Two
  options recorded, neither implemented: do the embedding call before
  opening the sandbox, or raise the busy-timeout.
- **`materials` has no uniqueness constraint on its de-dup key** (recorded
  2026-09-02, not built). A candidate migration `0013_material_unique_key`
  would add a unique index on
  `(category, standard_name, spec, unit)` — the same 4-tuple that
  `scripts/import_historical.py`'s find-or-create already treats as a
  material's identity. Today `models/material.py.__table_args__` carries
  only `ix_mat_prof_cat`, so nothing at the schema level stops the
  structural duplication that was cleaned up once already (8,303 → 6,288
  materials, a one-off `remediate_materials.py` on a since-deleted branch;
  the live DB re-measures 0 duplicate groups today, and the branch has been
  deleted because the historical-import path no longer produces them).
  **This is not a one-line migration** — two write paths would have to change
  in the same commit, and neither was written against this key:
  - `routes/materials.py::create_material` (`POST /api/materials`) inserts
    unconditionally with no duplicate check; under the constraint it would
    raise `IntegrityError` and surface as a 500. It needs an explicit
    conflict response (409 with the existing material) before the index can
    land. `update_material` (`PUT`) can likewise mutate a row into a
    colliding tuple.
  - `services/ingestion/import_service.py::_get_or_create_material` matches
    on `(category, standard_name, spec)` — **a different, looser key**: it
    ignores `unit` entirely, and drops the `spec` predicate altogether when
    `spec` is empty, so it can bind a quote to a material whose unit or spec
    does not match. Reconciling it with the historical-import key is a
    prerequisite, and is a correctness question in its own right
    independent of whether the index is ever added.
  Open question for whoever picks this up: whether the constraint should
  include `status`, since the de-dup key is scoped to `status='active'` in
  both the cleanup script and the measurements above, while a unique index
  would apply to soft-deleted rows too.
