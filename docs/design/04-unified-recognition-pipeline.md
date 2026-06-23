# Unified Recognition & Analysis Pipeline Design

> **Status — audited 2026-06-23.** Partially implemented; the architecture (page-role classification → extraction → canonical key → quality gate → anchor alignment) is in place, but the central model-selection claim diverges: production page classification uses **visual** models (`qwen3-vl-flash` flash pass + `qwen3-vl-plus` review pass on rendered thumbnails), **not** the rule + text-LLM (`qwen3.6-flash`) classifier this doc proposes.
> _Originally written during the early pipeline-redesign phase. English translation of the Chinese original; now the authoritative version._

> Stitch "recognition (OCR/extraction)" and "analysis (alignment/bid-comparison)" into one pipeline through a single **anchor key**.
> Design reference: fabric-bridge's "classify → extract-by-class → anchor normalization" architecture + 2025–2026 industry best practices for document intelligence processing.
> Background motivation in §1; the minimal landing slice in §8.

## 1. Background & problem

### 1.1 Two symptoms of the current state

1. **supplier_name polluted by brands**: the cover company name is not recognized, and the KITZ / Bermad values in the detail table's "brand" column get mistaken for the supplier name.
2. **Three suppliers cannot be aligned**: for the same tender, Kaishuo Xinzheng's quote has roughly twice as many line items as Taikelong's, so the system's alignment accuracy is inherently capped.

### 1.2 Root cause: doing "pairwise matching" with no anchor

The current alignment logic does **pairwise semantic matching** across multiple suppliers' quotes (guessing "A's line 12 = B's line 8"). This is exactly the n² pairing problem that the entity-resolution field explicitly warns against, and we don't even have a common baseline. It is like making someone reconstruct the exam questions from three answer sheets without ever reading the questions — barely workable, but with a ceiling on accuracy.

### 1.3 Key lessons from fabric-bridge

fabric-bridge (which likewise uses a DashScope Qwen VL OCR pipeline) **never does pairwise document matching**. Its architecture is:

```
Classify each page first (manufacturing instruction / special-order detail / spec sheet)
  → extract with a per-type dedicated prompt
  → normalize during extraction to a shared anchor key (hinban part number / 5-digit order number)
  → the two files cross-check and fill each other's fields via the anchor key
  → "analysis" = GROUP BY on the anchor key (CAD report = fabric_model+color_code aggregation)
```

The two order files are **two views** of the same shipment, cross-checked via hinban rather than matched against each other. The final report is just an aggregation.

### 1.4 Industry best-practice corroboration (2025–2026)

| Practice | Source conclusion | Implication for this system |
|------|---------|--------------|
| Classification is a single point of failure | Wrong document type → wrong schema → the whole extracted segment is void | Must do page-level role classification and prioritize its reliability |
| Separate pure extraction from post-processing | Extract only "what is visible"; do business inference separately to cut hallucination markedly | The extraction stage does no category inference / brand exclusion |
| Feeding OCR text + image together works best | Text gives content, image gives layout | Fall back to the original image for key fields |
| schema-first entity resolution | Have the schema (anchor) first, then match; embedding chunks replace string rules | Anchor-first, not pairwise matching |
| Validation failure → into manual review with structured errors | Keep the best-effort result as a review starting point; do not silently discard | Rows that don't reconcile go to the review queue |

## 2. Design principles

1. **Anchor normalization, no pairwise matching**: establish anchors first (tender-list rows, or canonical-key clusters); each supplier lands its rows against them.
2. **Classify first**: judge each page's role first, then decide its extraction and destination. Cover/bid-letter carry metadata (supplier name, total price); quote-list pages carry the detail.
3. **Separate pure extraction from post-processing**: extraction outputs only what is visible on the page; category inference, brand exclusion, and normalization happen in an independent post-processing layer.
4. **Tiered cost**: cheap models (OCR + text LLM) carry the full volume; the expensive general-purpose VL is invoked only on **validation failure**.
5. **Checksum-driven re-reading**: use "Σ detail == bid total" as the checksum for extraction completeness; only a failure triggers a VL re-read.
6. **Manual fallback, not silent discard**: items that fail validation/alignment enter the review queue with their reasons.

## 3. Unified pipeline overview

```
                          ┌─────────────────────────────────────────┐
   Bid PDF ──► render/page ─►│ ① Page-level role classification        │
                          │   cover/bid-letter · qualification · spec · quote list │
                          └───────────────┬─────────────────────────┘
                       ┌──────────────────┼──────────────────┐
                       ▼                                      ▼
          ┌────────────────────────┐          ┌────────────────────────────┐
          │ ② Metadata extraction   │          │ ② Detail extraction          │
          │   (non-list pages)      │          │   (quote-list pages)         │
          │   supplier_name / total │          │   qwen-vl-ocr → text-LLM struct │
          └───────────┬────────────┘          └──────────────┬─────────────┘
                      │                                        ▼
                      │                          ┌────────────────────────────┐
                      │                          │ ③ Normalize to canonical key │
                      │                          │   valve=(type,DN,PN,connect,material) │
                      │                          └──────────────┬─────────────┘
                      │                                          ▼
                      │                          ┌────────────────────────────┐
                      └─────────► total checksum ►│ ④ Validation system          │
                                                 │   per-page arithmetic + multi-page Σ==total │
                                                 └──────┬──────────────┬───────┘
                                                  pass  │         fail  │
                                                        ▼              ▼
                                          ┌──────────────────┐  ┌──────────────────┐
                                          │ ⑤ Anchor alignment │  │ ⑥ VL re-read fallback │
                                          │  list→match list  │  │  qwen3-vl-flash    │
                                          │  no list→canonical-key cluster │  │  read original image to recover failed pages │
                                          └────────┬─────────┘  └────────┬─────────┘
                                                   ▼                     │
                                          ┌──────────────────┐          │
                                          │ Bid-comparison matrix │◄─────────┘
                                          │ = anchor rows × suppliers │   reflow after re-read
                                          │   pure GROUP BY     │
                                          └────────┬─────────┘
                                                   ▼
                                          rows that don't reconcile → manual review queue
```

## 4. Per-stage detail (incl. model selection)

### ① Page-level role classification

**Purpose**: assign each page to a stable role. Roles are determined by the **tender process** (cover/bid-letter/qualification/spec/quote-list), stable across suppliers — this is the part that can be borrowed from fabric-bridge.

**Why no multimodal classification model is needed** *(corrected 2026-06-23: the production implementation does the opposite — it classifies visually with `qwen3-vl-flash` over rendered thumbnails and re-adjudicates low-confidence pages with `qwen3-vl-plus`; see `table_recognizer._classify_pages` and `dashscope_ocr.classify_pages_visual` / `review_pages_visual`. The rule-on-HTML classifier described below survives only as a coarse fallback (`page_classifier.classify_page`) used by the QuoteAdapter's `detect_pages` hook and meta routing, not as the primary router)*: each page has already run qwen-vl-ocr to produce HTML, so classification can run directly on that text. The distinction we need is coarse:

- **Quote-list page**: HTML contains a table whose header has columns like "unit price / quantity / spec / line total" → go to detail extraction
- **Non-list page**: no table or a sparse table → go to metadata extraction (cover/bid-letter for supplier name, total price)

**Model selection** *(corrected 2026-06-23: see the note above — the primary router is visual, not the rule + text-LLM path described here)*:
- Primary path: **rule-based judgment** (does the HTML contain a table with a price column) → near-zero cost, reliable
- Edge cases: **qwen3.6-flash (text)** classifying on OCR text, mirroring fabric-bridge's `CLASSIFY_PAGE`
- Only when cover OCR text is too sparse (big title image, table parsed as empty): fall back to **qwen3-vl-flash looking at the image**

> Do not use qwen-vl-max for classification — its visual-reasoning power is overkill and costly; this scenario does not need it.

### ② Role-based extraction

**Quote-list pages**: keep the existing two-stage `qwen-vl-ocr (table_parsing) → qwen3.6-flash structuring`.

**Non-list pages (cover/bid-letter)**: extract supplier_name and the **bid total price**. The supplier name is at a fixed position on the cover/bid-letter ("Bidder:", "Bidder organization name:"), so it no longer competes with the brand column. The bid total feeds ④ as the checksum.

**Pure-extraction principle**: this stage outputs only visible content; inference logic such as `_infer_category` and brand exclusion moves to an independent post-processing layer.

### ③ Normalize to a canonical key

Define a **canonical key** per category; extraction's job is to fill each row into this key. For valves, for example:

```
canonical key = (valve type, nominal diameter DN, nominal pressure PN, connection, body material)
e.g. (butterfly valve, DN100, PN16, wafer, ductile iron)
```

The canonical key is the anchor unit for downstream alignment. Granularity differences (Kaishuo lists per unit, Taikelong aggregates by spec) settle onto the same canonical key after normalization, and the 1:N relationship merges naturally.

### ④ Validation system (expanded in §6)

Per-page arithmetic validation + multi-page total checksum. **Checksum failure → triggers ⑥ re-read.**

### ⑤ Anchor alignment (expanded in §5)

With a tender list, use the list as the anchor; without a list, semantically cluster canonical keys to synthesize anchors. **Replaces pairwise matching.**

### ⑥ VL re-read fallback

**Trigger condition**: ④ validation fails (Σ detail ≠ total, or key fields missing).

**Model selection**: **qwen3-vl-flash (general-purpose VL) reading the original image**, not qwen-vl-ocr. Reason: a validation failure usually means table_parsing scrambled the table structure (merged cells, stamp occlusion causing row misalignment); switching to a "look-at-image-and-reason" general-purpose VL on the original image can recover it.

**Scope**: re-read only the failed page/region, not the whole document — most pages pass validation, so cost stays controlled. This is exactly fabric-bridge's Stage 3 pattern (crop the stamp region and re-read process_type with qwen3-vl-flash).

## 5. Anchor alignment strategy

### 5.1 With a tender list (main flow)

Each row of the tender list is one anchor row. Every detail line from each supplier is normalized to a canonical key and matched to its corresponding anchor row.

```
tender-list rows (anchors, vertical axis)
   × suppliers (horizontal axis)
   = bid-comparison matrix, each cell holding that supplier's quote for that anchor row
```

This is schema-first entity resolution: the tender list = schema/blocking key.

### 5.2 Without a tender list (degraded flow)

When there is no list, **synthesize anchors**:

1. Normalize all suppliers' detail lines into canonical keys
2. Semantically cluster the canonical keys (embedding chunks); each cluster = one anchor row
3. Each supplier lands onto the anchor rows

Tell the user explicitly: "this is estimated alignment; importing the tender list and re-aligning is recommended."

### 5.3 Why this solves the "cannot align" problem

No more pairwise matching — anchors first, then landing. Differing granularity no longer matters: multiple detail lines landing on the same anchor row become the same cell (with quantity merging). Alignment rate changes from "fraction guessed correctly" to "fraction that can land on an anchor" — measurable and optimizable.

## 6. Validation system

### 6.1 Per-page validation (pure code, no LLM, every page)

| Validation | Rule |
|------|------|
| Numeric coercion | qty / unit_price / total_price → float (reuse `_coerce_num`) |
| Row arithmetic | line total == unit price × quantity (within tolerance); flag mismatches |
| Noise-row filter | drop header, total, and subtotal rows |
| Schema validation | Pydantic; failures carry structured errors into review |

### 6.2 Multi-page checksum (pure code, per supplier) ⭐

```
Σ(this supplier's all detail line totals) == bid total declared on the cover/bid-letter ?
  ├─ equal (within tolerance) → extraction complete, release
  └─ not equal               → missing/duplicate/misrecognized rows, trigger ⑥ VL re-read + flag for review
```

This is the **single most information-rich validation** in the whole pipeline: the bid total is the checksum for detail extraction. It corresponds to fabric-bridge's `total_qty == sum(sizes)`.

### 6.3 Cross-document validation (optional, high value)

| View | Use |
|------|------|
| Bid file vs inquiry letter (second-round quote) | Same item, different price; verify the adjustment logic |
| vs tender-result approval sheet P5-P6 | Manual bid-comparison ground truth, for final reconciliation and evaluation baseline |

## 7. Model selection & cost tiering

| Stage | Model | Type | Trigger frequency |
|------|------|------|---------|
| Table OCR | qwen-vl-ocr (table_parsing) | dedicated OCR | every page |
| Structured extraction | qwen3.6-flash | text LLM | every list page |
| Page classification | rule + (edge) qwen3.6-flash | rule/text | every page |
| Per-page validation | — | pure code | every page |
| Multi-page checksum | — | pure code | per supplier |
| Canonical-key normalization | qwen3.6-flash or rule | text/rule | every detail row |
| Anchor clustering without list | embedding model (text-embedding) | embedding | only when no list |
| **VL re-read fallback** | **qwen3-vl-flash** | **general-purpose VL (image)** | **failed-validation pages only** |
| Cover name fallback | qwen3-vl-flash / text | VL / text | when supplier_name empty |

*(corrected 2026-06-23: the "Page classification" row no longer matches production — classification is performed by visual models `qwen3-vl-flash` (flash pass) + `qwen3-vl-plus` (review pass) on rendered thumbnails, run on every page rather than only on edge cases. The rule + text-LLM path remains only as a coarse fallback. The other rows — qwen-vl-ocr `table_parsing`, qwen3.6-flash text extraction, qwen3-vl-flash VL re-read — match `dashscope_ocr.py`.)*

**Principle**: the expensive general-purpose VL touches failed samples only; the full volume goes through the cheap OCR + text models.

## 8. Gap vs existing code / minimal landing slice

fabric-bridge is single-customer fixed-format, so it can afford hard-coded per-type prompts; we face arbitrary supplier formats, so **we cannot copy that part**. What we can copy is the "classify → extract-by-class → anchor normalize" skeleton. Holding to "don't over-engineer", **there are only three high-ROI minimal slices**:

| Slice | Solves | Current gap | ROI |
|------|------|---------|-----|
| **Page-level role classification** | supplier_name + filtering noise pages | currently only file-level doc_type (user picks manually), no page-level classification | High |
| **Canonical-key first** | precondition for alignment | extraction currently produces no canonical key | High |
| **Anchor alignment replacing pairwise matching** | three-way alignment | currently pairwise matching between suppliers | High |

Related changes already done:
- ✅ Multi-key rotation + 429 backoff retry (`dashscope_ocr.py`)
- ✅ `_pick_supplier_name` brand exclusion + cover-name fallback (`aggregator.py` / `pipeline.py`)

Not doing for now (low ROI or data-dependent): cross-document validation, fine-tuning of list-free semantic clustering, a 26SWP-style multi-format classifier.

## 9. Metrics

| Metric | Definition | Target |
|------|------|------|
| Total checksum pass rate | fraction of suppliers where Σ detail == bid total | primary extraction-completeness metric |
| Anchor landing rate | fraction of detail lines that can match an anchor row | primary alignment metric |
| supplier_name accuracy | fraction with the correct company name (not brand / not empty) | — |
| VL re-read trigger rate | fraction of pages triggering fallback | lower is better (cost) |
| Manual review item count | number of rows entering the review queue | lower is better |

## 10. To confirm / open questions

1. **Tender-list source**: for this batch of valve tenders' standard line items, is there a structured tender list usable as anchors? (`docs/项目资料/初始资料/` to be checked)
2. **Canonical-key fields**: the valve canonical-key field set needs business confirmation (does it include actuation type, flange standard, etc.).
3. **Total-price location**: is the bid total at a fixed position on the cover or the bid-letter? For multi-round quotes, which version governs?
4. **Approval-sheet ground truth**: can the horizontal comparison table on P5-P6 of the `tender-result approval sheet` be imported as the alignment-evaluation baseline?

---

**Reference sources**
- [Lessons from Running an LLM Document Processing Pipeline in Production (Alan)](https://medium.com/alan/lessons-from-running-an-llm-document-processing-pipeline-in-production-33d87f99cdb1)
- [Multi-Stage Field Extraction with OCR and Compact VLMs (arXiv 2510.23066)](https://arxiv.org/pdf/2510.23066)
- [OCR vs LLMs: Best Tool for Document Processing 2025 (TableFlow)](https://tableflow.com/blog/ocr-vs-llms)
- [The Rise of Semantic Entity Resolution (Towards Data Science)](https://towardsdatascience.com/the-rise-of-semantic-entity-resolution/)
- [Invoice matching: the procurement leader's 2026 guide (Amazon Business)](https://business.amazon.com/en/blog/invoice-matching)
- [Qwen-OCR / qwen-vl-ocr docs (Alibaba Cloud Model Studio)](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-ocr)
- Internal reference: `C:\Users\Justin\codes\repos\fabric-bridge` — `docs/technical-design.md`, `docs/prompts.md`
