# 10 - Unified Table-Recognition Base (procurement list + quote list shared)

> **Status — audited 2026-06-23.** Implemented. `ExtractionDraft` / `DraftRow` / `QualityReport` (`apps/api/intelligence/extraction_draft.py`) and the shared skeleton `recognize_tables` (`apps/api/intelligence/table_recognizer.py`) exist as designed; both the TenderAdapter (`tender_pdf.py`) and QuoteAdapter (`pipeline.py`) route through it, and adaptive tiling is built into this delivery. Matches current implementation; minor additions noted inline.
> _Originally written 2026-06-19 (per the tiling decision record in §3). English translation of the Chinese original; now the authoritative version._

## Goal & boundary

PDF table recognition for the procurement list (tender) and the quote list (bid) converges onto **one shared skeleton** + two-side adapters (prompt + hooks).
The shared skeleton owns all robustness, source evidence, and the quality-gate mechanism; each side only supplies "which pages / what prompt / what meta".

**This delivery = one task, with two internal acceptance segments (0+1); the shared base is complete only after both real-PDF paths pass.**
- Segment 0: extract the skeleton, wire up the tender side, and keep `test_extract_bidlist_real_pdf` behaviorally equivalent (regression guardrail).
- Segment 1: wire up the quote side; Taikelong real E2E passes.
- Long-term refactors that serve only code cleanliness without improving quote delivery are forbidden. The quote side gains in this delivery: an expected_rows retry gate, a quality report, bbox source, PASS/REVIEW/BLOCKED, and reconciliation — all substantive delivery improvements.

**Hard constraints**: do not modify the database; do not write fixed page numbers / fixed seq / fixed supplier logic; page location is always by signal scoring.

### Known-fact correction (from this repo's real E2E)
The tender side is **not a mature structural implementation**: the Jinqiao tender's 5 list pages had 0/5 TableGrid usage — all html_fallback.
It succeeds because it is a clean text-based PDF that the LLM reads directly from HTML. The structural-extraction layer (transposed / merged cells / cross-page continued tables) **has not yet been validated by any real sample**.
So what the skeleton extracts from the tender side is its **robustness scaffolding**, not its structural capability.

---

## 1. Shared schema: ExtractionDraft + QualityReport

The recognition path outputs `ExtractionDraft` (the recognition result, **unconfirmed**); it does not directly produce domain objects:

```
recognition → ExtractionDraft → user reviews & confirms → TenderAnchor / BidQuoteLine
```

### ExtractionDraft

```python
@dataclass
class DraftRow:
    row_index: int
    row_type: str           # quote_line|subtotal|grand_total|section_header|remark|invalid
    raw_cells: dict          # original OCR cell values (preserved, not dropped)
    fields: dict             # standardized fields (§4 data-model superset, see below)
    source_ref: dict         # {page, table, row, bbox}
    corrections: list        # [{field, raw, fixed, reason}] OCR-correction audit
    validation_flags: list   # [arithmetic_mismatch, missing_price, tax_basis_conflict, ...]

# fields keys (unified superset across both sides; missing → blank/None, never guessed):
#   name spec model pressure materials unit qty
#   unit_price_incl_tax unit_price_excl_tax tax_rate
#   total_price_incl_tax total_price_excl_tax brand profession remark canonical

@dataclass
class ExtractionDraft:
    doc_type: str                  # "tender" | "quote"
    source_file: str
    page_count: int                # total PDF pages
    processed_page_count: int      # must == page_count (page-count conservation)
    target_pages: list             # matched target table pages (1-based)
    rows: list                     # [DraftRow]
    meta: dict                     # per-side structure (tender brand / quote supplier+total)
    quality: "QualityReport"
    reconcile: dict | None         # content reconciliation when an Excel is present; else None
```

*(corrected 2026-06-23: the implemented dataclasses carry more than this sketch. `SourceRef` is a typed dataclass with an explicit `tile_bbox` field alongside `bbox`. `DraftRow` adds `field_sources`, `extra_fields`, and stores `parser_mode` inside `fields` ("llm" | "table_grid_deterministic"). `ExtractionDraft` adds `review_candidates: list[DraftRow]` — recall-page rows that fail the merge gate are isolated here and do NOT enter `rows` / bid-comparison / the database.)*

### QualityReport (the landing of CLAUDE.md §6 DocumentQualityReport)

```python
@dataclass
class QualityReport:
    status: str                    # PASS | REVIEW | BLOCKED
    total_pages: int
    processed_pages: int
    truncated: bool                # always explicit; hitting any cap sets True and degrades
    candidate_table_pages: list
    extracted_pages: list
    page_metrics: list             # [{page, role, input_mode, fallback_reason,
                                   #   expected_rows, extracted_rows, thinking_retry, tiled}]
    quote_line_count: int
    subtotal_count: int
    grand_total_count: int
    source_ref_coverage: float     # page/table/row coverage
    bbox_coverage: float           # single-column bbox coverage (incremental; not at 100% does not block, stays REVIEW)
    qty_parse_rate: float
    price_parse_rate: float
    arithmetic_consistency_rate: float   # qty×unit_price≈line total
    tax_basis_consistency: bool
    declared_total: float | None
    declared_total_diff: float | None    # detail sum vs declared total
    seq_missing: list
    seq_duplicate: list
    blocking_reasons: list          # why BLOCKED/REVIEW

# Status determination (thresholds centralized in config, §6):
#   BLOCKED: truncated and not fully processed; has quote data but quote_line=0; all rows skipped;
#            total/subtotal contaminating product rows; declared-total diff over threshold with no manual confirmation; key amount rows with no source.
#   REVIEW : expected/extracted gap, bbox not full, high html_fallback share, arithmetic consistency rate < threshold.
#   PASS   : none of the above.
```

*(corrected 2026-06-23: the implemented `QualityReport` is a superset of this sketch. It adds OCR-page accounting (`rendered_pages`, `ocr_success_pages`, `ocr_failed_pages`, `ocr_failed_indices`), an arithmetic-mismatch gate (`arithmetic_mismatch_count` / `_amount` / `_ratio` / `_rows`), and `failed_target_pages` (any target page raising an exception → BLOCKED — surfaces replay cache misses as real failures). The fields named `candidate_table_pages` / `extracted_pages` in the sketch are realized as `target_pages` + the per-page `PageMetric` list. Thresholds are centralized at the bottom of `extraction_draft.py` and pulled from `domain_config`: `_EXPECTED_ROWS_MIN_RATIO=0.70`, `_DECLARED_TOTAL_DIFF_BLOCKED=500.0`, `_DECLARED_TOTAL_DIFF_REVIEW=50.0`, `_REVIEW_PAGE_RATIO=0.30`, plus arithmetic-mismatch BLOCKED count/ratio/amount thresholds.)*

---

## 2. File boundary: skeleton + two-side adapters

| File | Role | Change |
|---|---|---|
| `apps/api/intelligence/extraction_draft.py` | **New**: ExtractionDraft/DraftRow/QualityReport dataclasses | new |
| `apps/api/intelligence/table_recognizer.py` | **New**: shared skeleton `recognize_tables(file, provider, adapter, progress_cb) -> ExtractionDraft` | new |
| `apps/api/services/tender_pdf.py` | shrink to **TenderAdapter** (detect_pages + prompt + meta); extraction logic moved out | greatly shrunk |
| `apps/api/intelligence/pipeline.py` | `_run_with_roles` / `_extract_page_with_html` **deleted**; `extract_quote` re-dispatches to skeleton + QuoteAdapter | dedup removed |
| `apps/api/services/tender_list.py` | `draft_row → TenderAnchor` (conversion after confirmation, **not inside recognition**) | small add |
| quote batch-confirm path | `draft_row → BidQuoteLine` (conversion after confirmation) | reuse existing |

### Adapter contract (minimal: 2 hooks + 1 prompt + 1 optional meta)

```python
@dataclass
class RecognizeAdapter:
    doc_type: str
    detect_pages: Callable[[list[str]], list[int]]   # hook ①: which pages are target tables
    row_prompt: str                                   # ②: Stage-2 extraction prompt
    extract_meta: Callable | None = None              # ③ optional: each side implements its own, no unified prompt

TenderAdapter: detect_pages=_score_page, row_prompt=TENDER_BIDLIST_PROMPT,
               extract_meta=tender-situation table (brand requirement + bidder brand)
QuoteAdapter:  detect_pages=page_classifier(QUOTE_TABLE/UNKNOWN), row_prompt=QUOTE_S2_TABLE_PROMPT,
               extract_meta=cover/summary (supplier + declared total + tax-price basis)
```

*(corrected 2026-06-23: the implemented `RecognizeAdapter` also carries `name_key` (tender="name", quote="material") and an optional `prompt_for_mode: Callable[[str], str]` so a side can use a different prompt per input_mode. The TenderAdapter's `detect_pages` is `_tender_detect_pages` (which wraps the `_score_page` signal scorer), and its `extract_meta` is `_tender_extract_meta`. The QuoteAdapter's `detect_pages` is `_quote_detect_pages`, which classifies via `page_classifier.classify_page` and selects `PageRole.QUOTE_TABLE` / `PageRole.UNKNOWN` — matching the sketch — and supplies `prompt_for_mode=_quote_prompt_for_mode` (table_grid → `_QUOTE_S2_TABLE_PROMPT`, else `_QUOTE_S2_PROMPT`).)*

Row → DraftRow standardization is done uniformly inside the skeleton (not a hook); mapping to domain objects happens after the user confirms.

### Skeleton fixed flow

```
recognize_tables:
  render all pages (MAX_PAGES_UNLIMITED, page-count conservation)
  ocr_pages_with_roles → (PageClassification, html)[]   # role + diagnostics
  target = adapter.detect_pages(htmls)
  for page in target:
      llm_input, expected_rows, input_mode, reason = build_llm_input(html)   # TableGrid/fallback
      data = llm_call(adapter.row_prompt, llm_input)
      if extracted < expected_rows * 0.7:                # expected_rows completeness check
          data = retry(...)                              # retry escalates the interface (see §3)
      rows += [to_draft_row(r, source_ref+bbox)]
  meta = adapter.extract_meta(...) if adapter.extract_meta else {}
  quality = compute_quality(rows, page_metrics, meta)    # PASS/REVIEW/BLOCKED
  reconcile = reconcile_vs_excel(rows) if xlsx else None
  return ExtractionDraft(...)
```

*(corrected 2026-06-23: the real `recognize_tables` flow is richer than this sketch, but the spine matches. Notable additions: lazy rendering — thumbnails are classified first, then only the pages OCR/orientation/Plus actually need are rendered at full resolution (Taikelong 53 pages dropped from a ~1.6GB peak); a three-phase visual classifier (Flash batch → Plus re-adjudication → semantic override); per-chain orientation correction (`_detect_chain_orientation`); tail-recall of mis-classified end-of-chain pages, kept isolated and best-effort; a per-page tax-field retry; cross-page dedup; missing-seq inference; and the arithmetic-consistency gate. The `0.7` expected_rows ratio and the build_llm_input → thinking retry → adaptive tiling escalation are exactly as drawn.)*

---

## 3. Adaptive tiling trigger conditions (**implemented within this delivery**)

**Decision (2026-06-19)**: full-page OCR on Taikelong is known to fail, cropping markedly improves it, and there is a line-by-line ground truth to accept against, so the data basis is in hand.
Tiling is a conditional degradation path inside the current delivery, not a future increment.

**Minimal reliable implementation (this delivery)**:
1. Judge orientation by page aspect ratio (landscape: W>H; portrait: H>W) and choose horizontal-strip tiling;
2. Cut N strips (default 4), each overlapping 10–15%;
3. Each strip records its bbox_pct = (x0, y0, x1, y1) fractional coordinates on the original page;
4. Each strip independently OCR → HTML → LLM extracts raw_items;
5. All strips' raw_items are sorted by tile_index and deduplicated with key (seq, name[:10], spec[:8]) (keeping the first seen);
6. Rebuild the unified raw_items list, then do field-semantic extraction and quality validation;
7. Each draft_row.source_ref appends `tile_bbox` to record its owning strip region.

*(corrected 2026-06-23: in the implemented `adaptive_tiler.tile_page` the strip orientation is the opposite of step 1's "always horizontal" — landscape pages are cut into **vertical** strips along x (good for transposed tables: row=attribute, column=material) and portrait pages into **horizontal** strips along y. Defaults are `DEFAULT_N_TILES=4`, `DEFAULT_OVERLAP=0.12`. Dedup key (seq, name[:10], spec[:8]) and the tile_bbox source-ref append match exactly.)*

**Trigger conditions** (any one fires it; ordinary pages don't trigger):
- after thinking-retry, still `extracted_rows < expected_rows * 0.7`;
- `input_mode == html_fallback` and `extracted_rows == 0` and the page has price signals.

*(corrected 2026-06-23: `_process_page` adds a third trigger — `fallback_reason == "no_grids"` and 0 rows and `table_count > 0` (table present but the parser failed, e.g. transposed/complex layout).)*

**Retry-exhaustion policy**:
- thinking-retry → tiling both fail → that page enters `REVIEW` (extracted_rows records the actual value);
- if > 30% of target pages enter REVIEW → the document is escalated to `BLOCKED`;
- emitting pseudo-complete results is forbidden (no filling with default values).

---

## 4. Real E2E acceptance table (tender + three quotes)

| Document | Pages | Ground truth | Segment 0 | Segment 1 | Key assertions |
|---|---|---|---|---|---|
| Jinqiao tender | 18 | Excel ✅ | **behaviorally equivalent** (89 rows/seq1-89/0-diff reconciliation unchanged after refactor) | — | source_ref 100%; reconcile 0 mismatch |
| Taikelong | 53 | Excel ✅(1,067,616.41) | — | **must pass** | 89 rows; tax-inclusive total diff ≤ 5 yuan; zero total contamination; source_ref{page,table,row} 100% |
| Kaishuo Xinzheng | 19 | Excel ⏳ | — | once Excel ready | same as above; declared-total closure |
| Shanghai Miancun | 31 | Excel ⏳ | — | once Excel ready | same as above; all 31 pages processed, no truncation |

Acceptance order (§13): page-count conservation → product-row completeness → row-level source → quantity/unit price/line total/tax basis → declared-total closure → only then look at alignment rate.
reconcile is optional: with an Excel, do content reconciliation; without an Excel, use declared total + arithmetic + sequence continuity + manual-confirmation closure.

---

## 5. Expected files to change / duplicated code removed

**New**:
- `extraction_draft.py` (schema, ~120 lines)
- `table_recognizer.py` (skeleton, ~200 lines; migrate tender_pdf's `_build_llm_input` / retry gate / `_compute_quality_metrics` in and add §6 status/financial fields)

**Shrink/remove duplication**:
- `pipeline.py`: delete `_run_with_roles`, `_extract_page_with_html`, the `_assign_source_ref_from_grids` call site (~−120 lines) → `extract_quote` becomes a ~15-line skeleton call.
- `tender_pdf.py`: `extract_bidlist`'s per-page loop / `_build_llm_input` / `_compute_quality_metrics` migrate into the skeleton; this file shrinks to the TenderAdapter (~−150 lines → adapter config).

**Untouched**: database, migrations, the `batch-confirm` write path (only attach `draft_row → BidQuoteLine` after confirmation), `reconcile_anchors` (reused).

**Net effect**: the two duplicate render→OCR→detect→per-page→merge skeletons merge into one; the quote side gains the retry gate / quality report / bbox / reconciliation.

---

## To decide
1. **tiling**: leave the interface slot now and implement as increment 2 (this design's recommendation)? Or build it into the skeleton now per Codex? *(resolved 2026-06-23: tiling was built into this delivery — see §3 and `adaptive_tiler.py`.)*
2. Proceed from the start as a single 0+1 task (Codex's proposal adopted).
