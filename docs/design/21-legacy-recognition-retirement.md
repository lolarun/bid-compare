# Retiring the Legacy Recognition Path

> **Status — superseded, 2026-08-11.** Phase 3's recommendation below ("do not delete, demote
> to cross-check", §5.3) was overtaken by facts discovered during the best-practice review
> (F1): both shipped providers (`DashScopeOCRProvider`, `MockProvider`) implement
> `vl_extract_csv` unconditionally, so the legacy branch was **never reachable in production**
> — there was no live second source to disagree with the VL path, so it supplied none of the
> cross-check value this section argued for. "Not reachable" is not the same claim as
> "reachable but degraded"; the argument below assumed the latter. On confirming this, legacy
> (`table_recognizer.py`, `table_parser.py`, `page_classifier.py`, `adaptive_tiler.py`,
> `aggregator.py`, `prompts.py`, `intelligence/snapshot_provider.py`, `splitter.py`, plus the
> dead call sites in `pipeline.py`/`tender_pdf.py`/`dashscope_ocr.py`) was physically deleted,
> not demoted. `.claude/rules/recognition.md` still needs the rewrite flagged in Phase 3 below
> (batch 4 of the review remediation) — its TableGrid/bbox language is now stale.
>
> The rest of this document (§1-§4, §5.1-§5.2, §5.4-§5.5, §6) is retained as the historical
> record of the VL-direct migration's reasoning and is otherwise still accurate.
>
> Basis: CLAUDE.md §4 / §6 / §8, `.claude/rules/recognition.md`, `.claude/rules/tests.md`.
> Companion: `docs/design/20` (checksum threshold), `HANDOFF.md` §0 (retracted conclusions).

## 0. Two corrections to the premise

**"OCR-HTML-TableGrid" names the path inaccurately.** `table_recognizer.recognize_tables`
returns an `ExtractionDraft` — the same type `vl_direct.build_draft` returns
(`table_recognizer.py:415-422`, `vl_direct.py:356-367`). `TableGrid` lives in
`table_parser.py:81` and is an intermediate structure, not the output. The two paths already
converge on one output type; the replacement is about *how the draft is produced*, not about
a type migration.

**Replacing legacy is not flipping `QUOTE_RECOGNIZER`.** VL-direct covers one of three entry
points, and on that one it currently lacks several independent checks that legacy supplies.
Those are enumerated in §2 and are the actual work.

## 1. Coverage today

| Entry point | Caller | VL-direct status |
|---|---|---|
| `extract_quote` (`pipeline.py:108`) | `document_ingestion.py` job type `QUOTE` | Branch exists, gated on `QUOTE_RECOGNIZER` + `hasattr(provider, "vl_extract_csv")`; **default legacy** |
| `extract_tender` (`pipeline.py:77`) | `document_ingestion.py:220` | **No VL branch at all** |
| `extract_tender_bidlist` (`pipeline.py:91`) | `document_ingestion.py:213` | **No VL branch**; `tender_pdf.py:298-302` raises `RuntimeError` unless the provider exposes `ocr_pages_with_roles` + `_llm_call_json` |

The tender side matters more than its share of the table suggests: it produces `TenderAnchor`,
which CLAUDE.md §4 defines as **the only row axis** for the procurement list and the matrix.
However accurate quote recognition becomes, the axis is still produced by legacy.

## 2. What legacy provides that VL-direct does not

Verified by reading the code; the three marked **(measured)** were reproduced directly.

### 2.1 Independent row-count evidence — the row ledger is inert on the VL path **(measured)**

`build_row_ledger` compares `expected_rows` against `extracted_rows` per page
(`extraction_draft.py:135-146`). On the legacy path `expected_rows` comes from the OCR `<tr>`
count — an **independent measurement** of how many rows the page contains. On the VL path
`vl_direct.py:336-342` sets `expected_rows`, `row_count` and `extracted_rows` all to the same
value: how many rows the model returned for that page.

Consequence: `dropped_rows` is structurally always 0, and `empty_pages` / `short_pages` are
always empty. Reproduced with a deliberate drop (3 pages declared processed, rows only from
page 1):

```text
{'target_pages': 3, 'expected_rows': 2, 'recognized_rows': 2,
 'dropped_rows': 0, 'empty_pages': [], 'short_pages': []}
```

**This retracts an earlier claim.** "Ledger: 137 rows in, 137 rows out, 0 dropped" was
reported several times during 2026-08-10 as evidence of recognition quality. It is a tautology,
not evidence. Recorded in `HANDOFF.md` §0.

Candidate replacements for the independent count, in order of cost:
- **Sequence continuity** — most documents carry a 序号 column; gaps reveal drops. Cheap, no
  extra model call, but absent on documents without a sequence column.
- **Subtotal reconciliation** — the prompt already asks for subtotal/total rows; a subtotal that
  does not equal the sum of the detail rows above it indicates a local drop. Independent of the
  declared grand total.
- **A second cheap pass** (thumbnail-level row counting) — real independence, real cost.

None of these is implemented. Until one is, the VL path has **no row-conservation evidence**,
and no report may claim otherwise.

### 2.2 Declared-total reconciliation is silently inert **(measured)**

`build_draft` accepts `declared_total` (`vl_direct.py:310`) and passes it to `compute_quality`,
which drives a BLOCKED/REVIEW gate (`extraction_draft.py:342-356`). `recognize_quote_vl` never
supplies it (`vl_direct.py:492-494`), so the gate never fires. Legacy fed it via
`_quote_extract_meta` → `provider.extract_doc_meta` (`pipeline.py:642-646`).

Separately — and this affects **both** paths — `document_ingestion.py:236-238` reads
`resp.metadata["doc_meta"]`, but nothing in `apps/api/intelligence` ever sets that key. Only
the xlsx bypass (`tabular_ingestion.py:363-379`) sets `_doc_meta`. So the declared-total
checksum consumed by `routes/analysis.py` and `services/quote_readiness.py` is already unfed
for PDF quotes today. That is a pre-existing defect, not one introduced by VL-direct, and it
should be fixed independently of this plan.

### 2.3 supplier_name is always empty on the VL path **(measured)**

Legacy has two mechanisms (`extract_doc_meta`, and the cover re-OCR fallback
`extract_supplier_name_from_cover` at `pipeline.py:315-324`). VL-direct passes nothing, and
`build_draft`'s default is `""`. Supplier identity is not a cosmetic field — it feeds supplier
resolution and, through it, submission identity.

### 2.4 Page-role classification

Legacy classifies pages visually with a flash→plus escalation and tail recall
(`table_recognizer.py:146-290`). VL-direct renders **every** page and hardcodes
`role="quote_table"` (`vl_direct.py:337`). For the seven benchmark documents this is
acceptable (they are mostly quote tables), but it means cost scales with total pages rather
than with relevant pages, and no page is ever excluded as a cover/certificate.

### 2.5 Smaller gaps

- `tile_bbox` (`table_recognizer.py:1489`) — VL has none. Note `bbox` proper is **never** set
  on either path, so `bbox_coverage=0` is already a permanent REVIEW hint for both.
- `draft.reconcile` (Excel reconciliation), `draft.review_candidates` — legacy only.
- Cross-page dedup, seq inference, tax-field sanity flags (`table_recognizer.py:1650-1689`,
  `1804`, `1893`) — legacy only.
- `input_mode="vl_direct"` is not in the frontend union type
  (`apps/www/src/api/client.ts:330` allows only `'table_grid' | 'html_fallback'`).
- The VL branch falls through to legacy when the provider lacks `vl_extract_csv`; the comment
  at `pipeline.py:117-120` mandates logging that, and **no log statement exists**.
- `MockProvider` implements the legacy surface only, so no test can exercise VL through
  `ExtractionPipeline`.

## 3. Proposed sequence

Each phase must produce evidence before the next begins. Phases 1 and 2 are independent of
each other.

### Phase 0 — close the silent gaps (prerequisite for any default flip)

1. Row-conservation evidence on the VL path (§2.1). Without it, "no rows dropped" is
   unfalsifiable and must not be reported.
2. Feed `declared_total` into `recognize_quote_vl` (§2.2).
3. Populate `supplier_name` (§2.3).
4. Log the legacy fall-through at `pipeline.py:121`.
5. Add `'vl_direct'` to the frontend `input_mode` union.
6. Give `MockProvider` a `vl_extract_csv` so the VL path is reachable in integration tests.

None of these requires a decision about legacy's fate; all are defects in the VL path as it
stands today.

### Phase 1 — quote side default flip

Blocked on **orientation stability**, not on Phase 0 alone. The seven-document evidence is
unambiguous: with correct orientation, results are near-exact (上海浦东 六次运行, best copy
136/136 rows, ±0.00 in four); with incorrect orientation, whole pages are lost. Orientation
detection remains unstable run-to-run (3/10 vs 10/10 on identical input). Flipping the default
before that is stabilised makes a high-variance component the production default.

Acceptance: fresh E2E on the seven documents, orientation consensus rate reported per run,
and row-conservation evidence from Phase 0 present.

### Phase 2 — tender side

`extract_tender_bidlist` needs a VL equivalent. The procurement list is structurally simpler
than a quote (no prices, no tax basis, no duplicate copies), but it **is the row axis**: one
wrong row affects every supplier's column. Needs its own golden set and its own A/B, not a
port of the quote prompt.

### Phase 3 — legacy's fate

**Recommendation: do not delete it; demote it to a cross-check.** The two paths fail
differently — legacy fails on OCR quality and table structure, VL fails on orientation and
output format. A document that VL marks BLOCKED can be re-read by legacy, and disagreement
between the two is itself a signal. Deleting legacy converts a two-source system into a
single-source one at exactly the moment we have documented that the single source has
unstable failure modes.

If deletion is nonetheless chosen later, `.claude/rules/recognition.md` must be rewritten in
the same change — it currently encodes the legacy architecture as normative ("表格结构优先
使用 OCR HTML 与 TableGrid", "可获得时保留 bbox 或 tile_bbox"). Per CLAUDE.md §5, code and
rules must not contradict each other.

## 4. Test baseline migration

Decided 2026-08-10: convert replay baselines to VL-direct rather than retire them.

**Done** — `test_cable_golden.py` B-layer (cases 4-7). Snapshot stores the model's raw CSV plus
the rotation map used; replay enters at `build_draft`, exercising the deterministic chain
(parse → column mapping → gates → draft) with no API and no rendering. Prompt-hash mismatch
**fails** rather than skips, per `.claude/rules/tests.md`. Recorder:
`scripts/record_vl_snapshots.py`.

One semantic change was required. The VL prompt deliberately emits **every** copy of a
repeated list (merging or dropping destroys evidence), while case 5 asserts no duplicates.
Copy selection therefore moved to the consumer: take the lowest `copy_no`. Selecting "the copy
closest to the declared total" was rejected as circular — it would pick the data using the
conclusion under test.

**Superseded, not done as VL conversion** — `test_e2e_snapshot.py` and
`test_compare_integration.py::TestPriceBasisBridgeFixtures` (formerly :1340,1557) replayed
legacy OCR snapshots through `SnapshotProvider`/`recognize_tables`, both deleted 2026-08-11
along with legacy. Rather than convert these to VL snapshots, they were deleted outright —
converting them would have meant recording new real-API VL snapshots for 3 more documents
(miancun/kaishuo/taikelong; only the 4 cable documents have VL snapshots so far), which needs
an explicit user-triggered run of `scripts/record_vl_snapshots.py`, not something to do
silently inside a legacy-retirement batch. This is a real, tracked coverage gap: full-stack
89-row/effective-total regression on 3 real historical documents, and 泰科龙's dedicated
`test_extract_quote_taikelong` fresh-E2E (also deleted, same reason). The synthetic contract
tests in `TestPriceBasisBridgeContract` still cover the price_basis logic itself, including the
exact 泰科龙 "含税合价还原单价" shape (`test_incl_unit_recovered_from_total_when_missing`).

`tests/fixtures/ocr_snapshots/` (7 files / 3.9 MB, 4 tracked in git) has no remaining reader
and can be deleted whenever someone wants the disk space back — not done here to keep this
batch to code, not fixture housekeeping.

## 5. Decisions taken 2026-08-10

### 5.1 Row conservation: sequence continuity now, second counting pass later — **implemented (part 1)**

Measured availability across the seven benchmark documents:

| Mechanism | Works on |
|---|---|
| Sequence continuity | **3 / 7** (凯硕, 泰科龙, 远东 carry a 序号 column; 上海浦东, 上海绵存, 亨通, 宏胜 carry **none**) |
| Subtotal reconciliation | **effectively 0 / 7** — no `subtotal` rows were emitted at all; five documents have a single `grand_total`, which is the existing checksum and adds no information |
| Second counting pass | 7 / 7, at the cost of one cheap call per page |

Subtotal reconciliation was therefore **rejected** — on this corpus it collapses into the
checksum already in place.

Implemented: `check_sequence_continuity` (`draft_integrity.py`), wired into `build_draft`.
Three deliberate boundaries:
- Coverage below `SEQ_COVERAGE_MIN` (80%) → verdict `not_applicable`. **This is not `ok`.**
  The draft is downgraded to REVIEW with reason `row_conservation_unverifiable`. If this
  returned `ok`, row conservation would be a tautology again — the very defect being fixed.
- No assumption that numbering starts at 1 or is globally monotonic; gaps are sought only
  **within the observed min..max**. Section-wise renumbering is normal in these documents.
- Duplicated sequence numbers are reported separately from gaps: a gap means a row is
  missing, a duplicate may be legitimate section renumbering.

Still outstanding: the second counting pass, for the 4/7 of documents with no sequence axis.
Until it exists, those documents have **no** row-conservation evidence and no report may
claim otherwise.

### 5.2 Page-role classification: not ported — **decided, no code change**

Rendering every page stays. Rationale: the dominant cost was the orientation pre-check payload
(12× the extraction), already reduced by 94%; page-role classification is itself a known
unstable component; and 泰科龙 (53 pages, all sent) produced 89 rows — correct, no phantom
rows from cover or certificate pages.

Recorded as a known risk: a certificate or annex page containing a table could contribute
phantom rows. Revisit if such an instance appears.

### 5.3 Legacy: demote to cross-check, do not delete — **superseded 2026-08-11, see status banner**

The two paths fail differently (legacy on OCR quality and table structure, VL on orientation
and output format), so disagreement between them is itself a signal. Deleting legacy would
convert a two-source system into a single-source one at the moment we have documented that
the single source has unstable failure modes (orientation 3/10 vs 10/10 on identical input;
extraction variance of 0.18% on 亨通 with identical orientation).

The cost — two paths and two test suites to maintain — is accepted.

**What actually happened**: this reasoning presupposed legacy was a *reachable, degraded*
second source. The best-practice review (F1) established it was unreachable — `hasattr(provider,
"vl_extract_csv")` was true for every shipped provider, so the `else` branch was dead code, not
a live fallback. A document VL marked BLOCKED was never actually re-read by legacy in
production; the "disagreement is a signal" mechanism described here could not fire. Deleting
unreachable code does not trade away a cross-check that was already absent. Physically deleted
2026-08-11; the orientation/extraction-variance risks named above are unaffected by this
correction and remain open (§6).

### 5.4 Checksum threshold: instrument now, change with the flip — **implemented (part 1)**

Neither ordering was acceptable on its own: landing the tight threshold before the flip would
apply it to legacy output whose residual distribution has never been measured; landing it
after leaves the riskiest window unprotected.

Split instead. Implemented now: `_build_checksum` logs `line_count`, `abs_delta`,
`delta_pct` and `per_row_delta` on every confirmation and returns them in its result. The
verdict is **unchanged** — still `delta_pct` against `CHECKSUM_BLOCK_DELTA_RATIO`. Zero risk,
and it yields the real distribution for both paths, including any documents whose declared
total legitimately includes out-of-list items (`docs/design/20` §5).

Still outstanding: the threshold change itself, to land in the same release as the Phase 1
default flip so the new default arrives with a gate matched to its residual.

### 5.5 Test layering: three layers, replay asserts determinism only — **implemented**

Recording the four cable documents produced two reds under the ported accuracy assertions:
亨通 off by 37,632 (0.18%) with **identical zero rotations** across runs — pure extraction
variance, not orientation — and 上海浦东 with 9 orientation pages unresolved. Freezing one
draw of a process with that variance makes the assertion a lottery: the same code is red or
green depending on which draw was frozen.

`.claude/rules/tests.md` already forbids the conflation ("snapshot replay 验证确定性，
fresh E2E 验证真实模型链路；三者不得互相冒充"). The ported B-layer was violating it.

| Layer | File | Asserts | API |
|---|---|---|---|
| A (1-3) | `test_cable_golden.py` | golden's own credibility | no |
| B (4-7) | `test_cable_golden.py` | **determinism**: same CSV → same draft; parser drops no rows; gates fire as recorded | no |
| C | `test_cable_accuracy_e2e.py` | **accuracy** vs golden, as an accumulated distribution | `@e2e`, opt-in |

Two of the B-layer assertions are genuine invariants (replay determinism; CSV data-row count
equals draft row count — a parser that loses rows is wrong regardless of model quality). The
third, "gates fire as recorded", is a **characterization baseline**: it pins current behaviour
including current defects. Its value is not "this is correct" but "a change here must be
noticed". Refreshing it costs nothing and calls no model —
`scripts/record_vl_snapshots.py --refresh-expected` replays the frozen CSV — so the diff of a
refresh *is* the behavioural change, reviewable as such.

C-layer deliberately asserts only catastrophic floors (row recall ≥ 80%, amount deviation
≤ 10%, price column mapped). It does **not** assert text fields: golden's text comes from a
transcription of the PDF into a reference CSV — human-verified, but still a second artifact,
and there is precedent for golden naming diverging from PDF literal text. Amount and row count
have an independent anchor (136 two-decimal rows summing exactly to the official total), so
conclusions are drawn only there.

## 6. Still open

- **远东 anomaly, unexplained.** Same document scored ±0.00 with **0 pages rotated** in one
  run and passed all cases with **14 pages rotated** in another. This contradicts "sideways
  pages cannot be read". Not to be used as the basis for any conclusion until explained.
- **Second counting pass** (§5.1) for the 4/7 documents with no sequence axis.
- **Phase 0 items 2-6** (§3): declared_total, supplier_name, fall-through logging, frontend
  `input_mode` union, `MockProvider.vl_extract_csv`.
