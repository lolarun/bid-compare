# Intelligence Layering of the Bid-Comparison Flow

> **Status — audited 2026-06-23.** Accurate and current; this is the authoritative architecture for the anchor-mode bid-comparison flow. §9 steps 1–3 (`tender_list.py`, `anchor_match.py`, alignment measurement) are implemented as described; step 4 (gate② LLM review + canonical-mapping cache) is genuinely deferred — present only as a docstring note in `anchor_match.py`, not implemented.
> _Originally written June 2026. English translation of the Chinese original; now the authoritative version._

> Answers an architecture question: in the whole flow of "tender procurement list → supplier quote → bid-comparison", what exactly should the LLM do, how much, and where does review happen.
> The goal is **one mechanism that covers all categories**, not one strategy per material.
> Upstream: `04-统一识别分析流程.md` (the overall recognition + analysis pipeline). This document focuses on how the "intelligence" within it is layered.

## 0. Core proposition

**Across the whole flow the LLM does exactly one thing: eliminate "expression differences" — collapsing the different ways of writing the same thing into a single canonical representation.**

```
Tender list ─┐                                ┌─ bid-comparison
             ├─► [canonical representation] ◄─── matching ───►├
Supplier quote ─┘                              └─ pure math, no intelligence
```

Bid-comparison itself needs no intelligence — by that point everything is already canonical. **All the intelligence is spent earlier, on "normalization".** This proposition decides: do not write a dedicated strategy for any single category; build one mechanism only for the general action of "normalization".

## 1. Four category-agnostic atomic operations

The "intelligence" of the whole flow decomposes into 4 atomic operations, which recur at different positions:

| Atomic operation | Essence (what difference it eliminates) | Primary owner |
|---|---|---|
| **extraction** | layout/scan → fields (eliminates layout differences) | LLM (scanned files) / code (Excel) |
| **normalization** | "真空磁环器"→"真空破坏器", "300*150"→"300×150" (eliminates writing differences) | embeddings + LLM, **zero hardcoded lexicon** |
| **matching** | this quote line = which anchor row (eliminates naming differences) | exact (code) → recall (embeddings) → decision (LLM) |
| **adjudication** | aggregation rows, 1:N line-item split, how to handle attribute conflicts | LLM + human |

**Why normalization cannot use a hardcoded lexicon**: a lexicon inevitably degenerates into "one strategy per material", which cannot cover the 342 material categories. Synonym / OCR correction is delegated to the AI semantic layer (embeddings + LLM), which is general across any category and is the only scalable approach.

## 2. Degree of intelligence: high in the middle, low at both ends

Intelligence is not uniformly distributed. Along the flow it forms an "inverted U":

| Flow step | Degree of intelligence | What the LLM concretely does | Determinism |
|---|---|---|---|
| ① Tender list parsing | **very low** | almost none; only recognizes column names when headers are odd | deterministic |
| ② Supplier quote extraction | **high** | after OCR, extract fields from HTML + row-by-row normalize spec/material/model into canonical attributes | non-deterministic → needs checksum |
| ③ Anchor matching | **layered** | exact matching runs in code (free); only the residue (synonym / OCR error / material disambiguation / aggregation) escalates to the LLM | non-deterministic → needs review |
| ④ Bid-comparison computation / alerting | **zero** | does not touch it at all; pure math | fully deterministic |

**Design principle: deliberately push work toward the cheap, deterministic two ends, and leave only the truly irreducible expression differences to the LLM.** Anything "solvable with code/embeddings" must not call the LLM.

## 3. Mechanism vs data: one engine, swap only data per category

The way to land "no one-strategy-per-material" = separating mechanism (code) from strategy (data):

| Layer | One general code path | Pure data/config that varies by category |
|---|---|---|
| List parsing | header detection + column-name synonym mapping (seq / name / spec / qty); remaining columns go into freeform attributes | does not hardcode column positions; relies on column-name recognition |
| Canonical key | the engine reads "which attributes are identity keys" | declared by attributes with role=match in `EXTENDED_ATTR_SCHEMAS` (valve = valve type/DN/PN/material; cable tray = spec/surface/thickness) |
| Normalization | AI semantic layer (embeddings + LLM) | **zero lexicon** |
| Matching engine | one layered algorithm | only swap the input schema; the algorithm is unchanged |
| Anchor source | one abstract interface | tender list (parsing) / no list (clustering synthesis) |

When a new category arrives, the questions are always the same set (what is the canonical schema, which attributes are identity keys), not "design another strategy".

**Already verified**: the 35 valve alignment groups produced by the fallback-path E2E contain not a single line of valve-specific code — they emerged from the general LLM aligner consuming the `category=阀门` parameter on its own. The general mechanism already exists; the anchor feature merely adds "the list" as an anchor source to it.

## 4. Anchor-source abstraction: anchor mode and fallback mode share one engine

The matching engine's input is "a set of anchor rows + a batch of quote rows to match". Where the anchor rows come from is replaceable:

| Mode | Anchor source | Alignment rate (measured/expected) | User hint |
|---|---|---|---|
| **anchor mode** | standard entries parsed from the tender list | expected 80%+ (to be verified) | normal |
| **fallback mode** | no list → cluster the quote canonical keys to synthesize anchors | measured 30% (bare) / ~50% (with normalization upper bound) | "Recommend importing the tender list and re-aligning" |

After either mode the matching, review, and bid-comparison are **completely identical**. Build only one matching engine; do not build a separate wheel for anchor mode.

## 5. Review architecture: three gates, two kinds of review

Normalization is done by the LLM and is non-deterministic, so **every normalization is a to-be-verified "claim"** that must pass a gate before it can be trusted, written into the bid-comparison matrix, and cached.

### 5.1 Review concentrates at the "matching boundary"

Normalization happens at two places (attribute normalization during extraction, row→anchor normalization during matching), but **a single review at the matching boundary covers both**: if attribute normalization is wrong (e.g. material 不锈钢↔UPVC), it hangs the row on the wrong anchor, which surfaces at the matching review. There is no need to review attribute by attribute.

### 5.2 Three gates

```
quote row ──normalize/match (proposal)──► candidate: hangs on anchor #55, conf=0.7
                                  │
        ┌─────────────────────────▼─────────────────────────┐
        │ Gate ① auto-validation (code, free, deterministic)  │
        │   is the normalized DN/material actually in the     │
        │   source text? anchor qty vs quote qty?             │
        └─────────────────────────┬─────────────────────────┘
                       pass │              fail → straight to human
        ┌─────────────────────────▼─────────────────────────┐
        │ Gate ② LLM review (an independent pass, adversarial)│
        │   swap prompt: "Does DN20 UPVC gate valve really    │
        │   equal anchor #55? Find a counterexample"          │
        │   run only on low-confidence or cache-miss; exact   │
        │   hits skip it                                      │
        └──────────┬──────────────────────────┬─────────────┘
       confirm high-conf│                  reject / still doubtful│
                     ▼                            ▼
            write to bid-comparison matrix + cache   ┌──────────────────────┐
            canonical mapping (skip LLM next time)    │ Gate ③ human review   │
                                                      │ (final authority)     │
                                                      │  see only the residue:│
                                                      │  rejected / material  │
                                                      │  conflict / aggregation│
                                                      │  adjudication = final  │
                                                      │  fact + cache          │
                                                      └──────────────────────┘
```

### 5.3 Positioning of the two kinds of review

| | LLM review (Gate ②) | Human review (Gate ③) |
|---|---|---|
| Position | after the match proposal, before writing the matrix, an automatic pass | residue queue after LLM review rejects/doubts |
| Trigger | low confidence or cache miss (exact hits skip) | only what even the LLM dares not confirm |
| Nature | an independent second pass, adversarially finding counterexamples (**not** the same call) | system final authority; adjudication = final fact |
| Output | confidence; high → release, low → demote to human | confirmed result settles into the cached canonical mapping |
| Scale | the residue volume, falling as the cache thickens | very few, target trending to zero |

## 6. Canonical-mapping cache: review once, the LLM share shrinks over time

Whether by LLM or human, once a mapping is confirmed ("真空磁环器→真空破坏器", "this UPVC gate valve→anchor #55"), it is written into the **canonical-mapping cache**. Next time the same expression hits directly, no LLM call and no asking a human.

- Cold start: high LLM share (no accumulation)
- After running for a while: the cache thickens → the exact/rule-layer hit rate rises → the LLM only handles genuinely new expressions

This is critical to the cost of the 342 material categories: **otherwise every tender calls the LLM in full, expensive and slow. The more the system is used, the less it depends on the LLM.**

## 7. Current state vs to be newly built

| Component | Current state | Action |
|---|---|---|
| General LLM aligner (category parameter) | ✅ exists, usable in fallback mode | reuse |
| Bid-comparison matrix / alerting (pure math) | ✅ exists | reuse |
| Human confirm/reject UI + confidence | ✅ exists (prototype of Gate ③) | reuse, wire into the residue queue |
| Tender list parsing (general table → anchor rows) | ❌ none | **new build** |
| Anchor-mode matching (quote → fixed anchors) | ❌ none (currently emergent clustering) | **new build** |
| Gate ② independent LLM review | ❌ none | **new build** |
| Canonical-mapping cache | ❌ none | **new build** |
| Canonical-key schema (mark role=match) | ⚠️ partial (`EXTENDED_ATTR_SCHEMAS` has attributes, identity keys not marked) | **add annotation** |

## 8. To be confirmed / open questions

1. **Cache key granularity**: does the canonical-mapping cache hit by "raw expression string" or by "nearest neighbor of the raw expression's embedding vector"? String-exact is fast but only blocks fully identical; embedding-neighbor can block similar variants but risks mis-hits. Recommend string-exact first, observe the hit rate, then decide whether to adopt embeddings.
2. **Trigger threshold for Gate ② LLM review**: below what conf should review run? Needs calibration on this tender's data (confidence distribution vs actual error rate).
3. **List-parsing fallback**: ~~if the customer's list has no standard header, fall back to the LLM reading the header~~ — **decided (2026-06-08): the first version does not handle irregular headers; it only consumes standard headers to get the trunk working**. Odd-header LLM fallback is left to a later iteration.
4. **Ownership of the canonical-key schema**: is the identity-key annotation added to the existing `EXTENDED_ATTR_SCHEMAS`, or a separate "category identity key" config? (Leaning toward the former, to avoid two configs drifting.)

## 9. Implementation order

In the prudent order of "quantify first, productionize later":

1. ✅ **List parser** (`apps/api/services/tender_list.py`) — measured: valve list 90/90 rows, total 1745 exact, material disambiguation correct (gate valve DN20 splits 不锈钢#45/UPVC#55)
2. ✅ **Anchor-matching measurement** (`scripts/measure_anchor_alignment.py`, embedding recall + DN check, no LLM no lexicon) — see §9.1 measurements
3. **Production endpoint (in progress)**: upload list → build anchors → embedding match → land `BidAlignmentGroup` → bid-comparison matrix. **Gate ② LLM review + cache deferred**, added later once real usage shows the review volume (avoid over-design). Decided 2026-06-08.
4. (Deferred) Gate ② LLM review + canonical-mapping cache + frontend residue queue

### 9.1 Anchor-matching measured baseline (2026-06-08)

Data: project 60, three valve quotes (82/87/44 = 213 lines) against 90 anchor rows.

| Metric | Bare fallback alignment | Anchor mode (embedding + DN check) |
|---|---|---|
| Quote match rate | 30% | 100% (recall, includes need-review) |
| Comparable anchors ≥2 suppliers | — | **65/90 = 72%** |
| Three-supplier-complete rows | 3 | **19** |
| Confidence distribution | — | ≥0.85: 128 / 0.70-0.85: 83 / <0.70: only 2 |

Verification conclusions: ① embeddings with zero lexicon can do general normalization (synonym 0.99, material disambiguation UPVC↔不锈钢 both correct); ② **Gate ② review is indeed necessary** — when the list has no corresponding anchor (球墨铸铁 EPDM DN25), embeddings force-hang the nearest one, and a mis-match can only be intercepted by review; ③ the review volume is small (only 2 suspect).
Caveat: the 72% is "≥2 comparable + high confidence"; precision has not yet been ground-truth-verified against the approval-form truth set.

### 9.2 Inherent difficulty of supplier_name extraction (2026-06-08)

A bid document contains 4 kinds of company names: the tenderer (party A), the bidder (seller), the manufacturer/brand owner (e.g. 开滋 KITZ), and the agent.
Pure text alone cannot reliably distinguish them — the bidder's real name is often deep in a stamped page, and OCR will lose the "bidder unit name" label
(measured: on page 9 the label was truncated to "称"). The handling strategy follows "blank > wrong":
- Bidder label on the cover and clear (绵存 / 泰科龙) → extracted correctly
- Label lost by OCR (凯硕) → return blank, defer to the filename fallback or manual fill-in, **never confidently fill it in as the tenderer/manufacturer**

The prompt already explicitly excludes tenderer/manufacturer/agent; the deep scan stops early after the first 10 pages. A thorough fix requires a VL model to read the stamp region (image level, like the fabric-bridge stamp fallback); ROI is low and is deferred for now — supplier_name is a single field that a human can fix in 2 seconds.

---

**References**
- `docs/design/04-unified-recognition-pipeline.md` — upstream overall pipeline (this document is the refinement of its stage ⑤ + review)
- `docs/design/01-material-master-data-standard.md` — standard-compliance matching priority (exact > rule > AI > human), `EXTENDED_ATTR_SCHEMAS`
- Measurement basis: fallback path E2E (35 groups / 30% alignment rate), tender list (90 anchor rows / 18 valve types / 15 repeated keys needing material disambiguation)
