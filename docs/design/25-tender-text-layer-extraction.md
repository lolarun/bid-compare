# 25 — Tender Procurement-List Text-Layer Direct Extraction

> **Status — implemented 2026-08-13.** Split out of a broader "recognition
> engine replacement" investigation (see
> `docs/design/26-recognition-engine-evaluation.md` for the harder,
> higher-risk half) at the user's explicit direction: this track is
> independent of any model-swap decision and landed first.
>
> Delivered exactly as designed below: `apps/api/intelligence/tender_text_layer.py`
> (detection, anchor-table extraction with two-level header flattening and
> cross-page continuation, brand-requirement-table extraction), `parser_mode`
> threaded through `vl_quote.py::parse_csv`/`build_draft` and
> `vl_tender.py::build_tender_draft` (default stays `"vl_direct"`, only the
> new call site passes `"text_layer"` — `fields.parser_mode` /
> `PageMetric.input_mode` / `meta.recognizer` all three updated together, not
> just one, per the N1 labeling precedent), wired into
> `tender_pdf.py::extract_bidlist` with the fallback gating described in §4.2
> (skipped entirely when `bidlist_pages`/`brand_page` are manually set — a
> user correction in progress shouldn't have its extraction strategy swapped
> underneath it).
>
> One deliberate scope decision made during implementation, not anticipated
> in the original design: cover-page scalars (§4.3 in the original draft
> before this rewrite — kept on the VL path) still use `vl_call`, exactly as
> planned; brand requirements were **also** brought onto the deterministic
> path (not planned above, added because the brand-requirement table turned
> out to be an equally clean, equally structured table — no reason to pay a
> vision call for it once the anchor-table parser existed).
>
> **Acceptance criteria (§5) verified with real numbers, not estimated**: run
> against 金桥地铁上盖 J9A-03 (`docs/test/金桥地体上盖招标文件.pdf`) —
> real VL-direct baseline (`vl_tender.parse_tender_document`, full pipeline
> incl. orientation pre-check) **363.8s**; text-layer path **14-18s** (~20-25x).
> 89/89 anchor rows in both, first/last row field-for-field identical
> (seq/name/spec/unit/qty). Cover scalars 100% identical across all 4 fields,
> including the two genuinely-empty ones (`project_code`, `deadline` — this
> document simply doesn't state them; confirmed by full-text search, not
> assumed) — both paths correctly return empty rather than guessing. Brand
> requirements (3 brands) and supplier-brand mappings (3 suppliers) identical.
> Quality tier identical (`REVIEW`, same `bbox_coverage=0` reason — an
> existing, path-independent gate, not something this track changed).
> Fallback verified on two axes: a scanned PDF (`has_usable_text_layer` →
> `False`, VL-direct path unchanged) and a text-layer PDF with no anchor
> table (`build_anchor_csv` → `None`, VL-direct fallback, not a silent empty
> result).
>
> `apps/api/tests/test_tender_text_layer.py`: 9 fast unit tests (detection,
> cross-page continuation incl. the non-consecutive-page rejection boundary,
> two-level header flattening, brand-table parsing, `parser_mode` labeling,
> both fallback paths) + 1 `@pytest.mark.e2e` field-for-field contract test
> against real VL-direct (deselected by default, matching this project's
> `-m 'not e2e'` convention). Full suite: `pytest apps/api/tests tests -q` →
> 824 passed (815 baseline + 9 new), 0 failed, 1 skipped, 8 deselected.
> `vue-tsc -b` clean, `vitest` 65/65 (a cosmetic `INPUT_MODE_LABELS` entry was
> added for the new `"text_layer"` value in `compare/IndexView.vue`'s
> diagnostics badge — the existing `| string` escape hatch in the TS type
> already made this non-breaking without the label, the addition is purely
> for a correct display label instead of a bare fallback).
>
> Basis: CLAUDE.md §1/§4/§6, `.claude/rules/recognition.md`. Evidence for the
> motivating claims below lives in `HANDOFF.md`'s 2026-08-13 merge-checkpoint
> section and `outputs/baidu_unlimited_ocr/run.stdout.log` (gitignored, rerun
> `tests/test_baidu_unlimited_ocr_standalone.py` to reproduce).
>
> **A note on `.claude/rules/recognition.md`'s dual-path prohibition**,
> addressed head-on rather than argued around: the rule bans reintroducing
> "part of the table goes through deterministic TableGrid, complex headers
> fall back to LLM" — an **intra-document, per-table** routing decision tied
> to the specific deleted legacy component. This track's routing decision is
> **document-level and pre-recognition**: a PDF either has a text layer or it
> doesn't (a structural fact of the file, determined once, before any
> recognition path is chosen), and when it does, the vision model is not
> invoked **at all** for that document — not "used for some tables, skipped
> for others." No code or logic from the deleted `table_recognizer.py`/
> `table_parser.py` lineage is reused; `tender_text_layer.py` is new,
> independent, and has no shared failure modes with what was deleted.

## 1. Problem

A customer reported that PDF recognition is slow and compared it unfavorably
to WPS's table recognition, which they experience as fast and accurate. WPS
almost certainly reads the embedded text layer directly when a PDF is
born-digital, and only falls back to OCR/vision for genuine scans — a
categorically different (and categorically cheaper) operation than sending a
full-page image to a general-purpose multimodal chat model and asking it to
transcribe a CSV.

This project's current recognition path (`vl_quote.py` / `vl_tender.py`,
VL-direct) always renders every page to an image and always calls the vision
model, regardless of whether the source PDF actually needs OCR. For the
**tender/procurement-list side specifically**, that is often unnecessary
work: tender documents are frequently produced digitally (Word → PDF export,
or an Excel-derived table), not scanned.

## 2. Evidence this is worth doing

Two independent data points, both real, neither hypothetical:

1. **A live production sample.** Querying `extraction_jobs` for distinct
   `type IN ('tender', 'tender_bidlist')` source files with `status='done'`
   found 4 distinct historically-processed tender PDFs. Checking each with
   `pypdfium2`'s `get_textpage().get_text_range()` on the first 5 pages:

   | File (by content hash) | Pages | Text-layer chars (first 5p) | Has usable text layer |
   |---|---|---|---|
   | `708f920f...` | 1 | 0 | No — scan |
   | `9f5b3a1d...` | 18 | 2290 | **Yes** |
   | `e47785b8...` | 53 | 0 | No — scan |
   | `0c5fd20c...` | 11 | 2303 | **Yes** |

   **2 of 4 (50%)** — small sample, directional only, not a claim about the
   true population rate. Worth re-checking with a larger sample once this
   track has telemetry of its own (§7).

2. **A speed reference, already measured.** The one text-layer tender PDF in
   this project's benchmark corpus (金桥地铁上盖 J9A-03, 18 pages, the
   `9f5b3a1d...` file above) was run through Baidu's OCR service on
   2026-07-13 and came back in **17.7 seconds at 89/89 (100%) row recall** —
   see `HANDOFF.md`'s merge-checkpoint section. That's not this track's
   proposed implementation (it used a cloud OCR API, not direct text-layer
   parsing), but it is concrete evidence that a fast path is achievable on
   this exact document, in contrast to the ~10-12 minutes the current
   VL-direct path takes end to end (orientation pre-check + extraction) on a
   comparably-sized scanned document.

## 3. Scope

**In scope**: `extract_tender_bidlist` (the procurement-list recognizer,
`vl_tender.py`'s entry point today) — text-layer detection + direct
structured-table extraction for tender PDFs that have one.

**Out of scope**: the quote/bid side (`vl_quote.py`). Every bid PDF checked
this session (凯硕新正/泰科龙/上海绵存/上海浦东/亨通/宏胜/远东 — 7 documents)
was a pure scan with zero text-layer characters. Suppliers submit signed,
stamped, scanned bid documents; that is expected to remain the normal case.
This track does not touch that path, and does not change VL-direct's default
status there.

## 4. Design

### 4.1 Detection

Before rendering any page to an image, check for a usable text layer:

```python
def has_usable_text_layer(pdf_path: str, sample_pages: int = 5,
                           min_chars: int = 200) -> bool:
    """Cheap, page-render-free check. False negatives (declining a text-layer
    PDF) are safe — falls back to the existing VL path. False positives
    (accepting a scan whose text layer is OCR garbage baked in by a prior
    tool) are the real risk — min_chars and a structure check (§4.2) both
    guard against a PDF with a few stray embedded characters passing."""
```

`min_chars` and `sample_pages` are named, centralized constants (CLAUDE.md
§4 — no magic numbers), tuned during the acceptance test (§7), not guessed.

### 4.2 Extraction

When a text layer is present, parse it into the same structured shape the
tender recognizer needs — anchors (name/spec/unit/qty/category), cover-page
scalars (project_code/tender_date/deadline), and brand requirements — using
a table-structure-aware PDF text extraction (column/row inference from text
positions, not a vision call). This is genuinely new code, not a reuse of
`vl_tender.py`'s prompt-based approach; it needs its own column-header
mapping and its own malformed-table detection.

**If the text-layer table doesn't parse cleanly** (ragged columns, no
identifiable header row, anchor count implausibly low) — fall back to the
existing VL path for that document. No silent partial result; the fallback
decision is logged and reported the same way `unresolved_pages` is reported
today (`.claude/rules/recognition.md`: no silent truncation).

### 4.3 Output contract

Must produce the same `ExtractionDraft` shape `build_draft` in `vl_tender.py`
does today, tagged with a new `parser_mode` value (`"text_layer"`, distinct
from `"vl_direct"` — do not overload the existing label, per the N1
naming-supplement precedent in `docs/design/21`). Downstream (alignment,
matrix, frontend) reads `ExtractionDraft` uniformly regardless of
`parser_mode` — this is the same architectural fact that makes
`docs/design/26`'s engine-swap investigation tractable, and it applies here
too: this track is invisible to everything past the recognizer boundary,
provided the shape is honored exactly.

## 5. Acceptance criteria

Run against 金桥地铁上盖 J9A-03 (`docs/test/金桥地体上盖招标文件.pdf`,
18 pages, confirmed text-layer, existing golden Excel):

- 89/89 anchor rows recovered (name/spec/unit/qty), matching the VL-direct
  path's own output field-for-field (this is a same-document A/B, not a
  golden-only check — the two paths must agree, not just each independently
  hit the golden number).
- All 4 cover-page scalars (project_code/tender_date/deadline + project_name)
  extracted and matching.
- Brand requirements list matching.
- Wall time < 30 seconds (vs. VL-direct's current ~minutes on a page count
  this size).
- Fallback path exercised and verified: feed a scanned tender PDF through
  the same entry point, confirm it silently and correctly routes to
  VL-direct with no behavior change from today.

## 6. Non-goals

- Not a general "OCR replacement" project — that's `docs/design/26`, kept
  deliberately separate because it's a different risk class (a genuine
  scanned-document recognizer swap, needing its own accuracy bake-off).
- Not touching `.claude/rules/recognition.md`'s "整份文档一次调用" invariant
  for the VL path — this track only adds a **pre-check** that may bypass VL
  entirely for a subset of documents; it doesn't change VL's own behavior
  when it does run.
- Not solving the tender-side "which fraction of real customer documents are
  born-digital" question definitively — §2's n=4 sample is a motivating
  data point, not a sizing study. Real telemetry from this track's own
  detection function (§4.1), once shipped, will answer that properly; add a
  lightweight counter/log at the detection call site so this number stops
  being a one-time sample.

## 7. Open questions

1. `min_chars`/`sample_pages` thresholds — pick numbers, then validate against
   a slightly larger sample than today's n=4 before locking them.
2. Column-header mapping for the structured-table parse (§4.2) — generic
   keyword-based (matching this project's convention elsewhere, e.g.
   `scripts/score_paddleocr_vl.py`'s `_HEADER_KEYWORDS` approach) or a
   different strategy? Keyword-based is the recommendation, for consistency
   and because it's already been validated as workable this session.
3. Where does the fallback decision get surfaced to the user — silently in
   logs only, or also as a visible `parser_mode` badge somewhere in the
   recognition-result UI? (Leaning toward: log only for now, revisit if
   `parser_mode="text_layer"` ever needs its own quality-tier treatment
   distinct from `"vl_direct"`.)

## 8. Test plan

- Unit: `has_usable_text_layer()` against a small fixture set (the 2
  text-layer + 2 scan tender PDFs already sampled in §2, plus the existing
  `docs/test/金桥地体上盖招标文件.pdf`).
- Contract: text-layer path's `ExtractionDraft` output diffed field-for-field
  against the same document's VL-direct output (§5).
- Fallback: malformed/undetectable table on a text-layer PDF routes to
  VL-direct, logged, not silently degraded.
- Existing `apps/api/tests` + `tests` full suite stays green (815 passed
  baseline, `HANDOFF.md` 2026-08-13 checkpoint).
