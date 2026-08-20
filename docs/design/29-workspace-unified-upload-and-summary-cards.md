# 29 — Unified Upload Zone + Summary Cards (workspace redesign, round 2)

> **Status — cut 1 measured and paused 2026-08-20; cuts 4-5 proceed
> independently.** Follows design/27 (workspace shell) and design/28
> (Tier 0/1 classification). Extends both rather than replacing them — the
> classification ladder and the QuoteGrid review surface stay.
>
> **Cut 1 (scanned-PDF Tier 1.5) result**: measured against the real corpus
> per §3's own requirement — **0/7** on real scanned bid PDFs, and not
> random noise: both inspected failures show the *same* systematic
> misreading (see §3.1). This is a real, measured finding, not something to
> route around with prompt tweaks. User decision (2026-08-20): **pause this
> piece, keep the three precise upload cards (design/28 cut 5) as the entry
> point for PDFs** — do not retire them (§6 was already gated on this
> measurement passing). Cuts 4-5 (summary cards, stats) do not depend on
> cut 1-3 and proceed on their own.
>
> The code (`apps/api/intelligence/scanned_pdf_classify.py`,
> `DashScopeOCRProvider.classify_document_kind`) is kept, not deleted — it's
> real, tested infrastructure (native-PDF path works, cover-region keyword
> fix verified against real fixtures) — just not wired into the upload
> dispatch path given the measured accuracy. Left for a future revisit with
> a different signal (§3.1 has some starting ideas), not treated as dead
> code to clean up.

## 1. Trigger

User (2026-08-20), reviewing the shipped design/28 workspace, asked for three
changes:

1. Collapse the three precise upload cards (招标文件/采购清单 Excel/投标文件)
   into the single auto-classify drop zone — one entry point, not four.
2. Default view under each supplier/tender slot becomes a **summary card**
   (LLM-composed overview), not the raw line-item grid.
3. Each card shows 采购清单数量 / 报价清单数量 / 报价总计 underneath.

Three clarifying decisions made in discussion (2026-08-20):

- **D1 — scanned-PDF classification**: build real Tier 1.5 classification
  (chosen over the cheaper two-choice-prompt fallback) so the single drop
  zone works for scanned PDFs too, not just Excel.
- **D2 — detail grid**: stays, reachable by clicking into a card. Not
  removed, not the default landing view.
- **D3 — 报价总计 basis**: sum of *all* rows including pending/unconfirmed,
  clearly labeled as a rough total, not the official evaluation number.

## 2. Why design/28's cut 5 couldn't already do this

Cut 5 (shipped) intentionally kept the three precise cards as a fallback
because Tier 0 has **zero signal** for scanned PDFs — it can only tell
`native` vs `scanned` text layer, not `tender` vs `bid`. The cut 5 scope note
said as much: full auto-dispatch for scanned PDFs was deferred as "risk too
large without more validation" (see design/28 §9). This round is that
validation + implementation.

The real constraint: recognition needs to know **upfront** whether to run
the tender-extraction pipeline or the quote-extraction pipeline — they are
different prompts/schemas (`vl_tender.py` vs `vl_quote.py` / `paddle_tender.py`
vs `paddle_vl.py`). You cannot pick after full extraction without either (a)
running both pipelines on every file (2x cost, wasteful) or (b) a cheap
pre-classification pass before dispatch.

## 3. Design: two-tier pre-dispatch classification

```
PDF dropped into unified zone
  │
  ├─ has_usable_text_layer? (design/25, already built)
  │
  ├─ YES (native) ─────────────────────────────────────────┐
  │   Reuse tender_text_layer.py's existing fast text        │
  │   extraction (deterministic, zero model calls, already   │
  │   proven 14-18s vs 363.8s VL-direct). Run the SAME        │
  │   keyword/structure heuristic design/28 Tier 0 already    │
  │   uses for Excel (look for 招标编号/投标单位/供应商 cover  │
  │   markers in the extracted text) — no new model call.     │
  │                                                            │
  └─ NO (scanned) ─────────────────────────────────────────┐  │
      New: **Tier 1.5** — one targeted, cheap model call on   │
      page 1 only (not the full multi-page pipeline): "招标 /  │
      投标 / 不确定" + best-effort project_name/supplier_name. │
      Distinct from Tier 1 (which reads POST-recognition       │
      signals) — this runs BEFORE dispatch, on minimal input.  │
      "不确定" is a valid, expected answer (same precedent as   │
      Tier 0's ambiguous-Excel case) — surfaces a manual        │
      2-choice prompt, doesn't guess.                           │
                                                                 │
  ┌──────────────────────────────────────────────────────────┘
  ▼
Dispatch to the correct existing pipeline (tender OR quote extraction) —
no double-extraction, no new extraction logic, only the ROUTING decision
is new.
```

**Must-measure before shipping** (matching design/28 §2's own precedent —
"measured, not assumed"): run Tier 1.5 against the real scanned-PDF corpus
(泰科龙/绵存/凯硕 bid PDFs + 金桥/prj2 tender PDFs, already in
`tests/fixtures/documents/`) and report actual accuracy + added latency
before treating this as production-ready. If accuracy is poor on any
document class, that's a real finding to report, not something to route
around by lowering the bar.

**Cost note**: this adds one small model call per scanned-PDF upload (page 1
only) ahead of the existing full-document call. Given the existing full
pipeline already costs far more (multi-page OCR + extraction), this is a
marginal addition, not a new cost tier.

### 3.1 Measured result (2026-08-20) — 0/7, paused

Ran the page-1-only vision classifier (`classify_document_kind`) against all
7 real scanned bid PDFs in the corpus (泰科龙/绵存/凯硕 + prj1 浦东/亨通/
宏胜/远东). **0/7 correct.** Native-PDF path (`classify_native_pdf`,
zero-model-call keyword judge) works correctly on both real native tender
PDFs once fixed to scan only the cover region before the table-of-contents
marker (an earlier version scanning the whole first 2 pages hit both
tender/bid keyword sets — tender documents' own table of contents lists
chapters like "第四章 投标须知", which legitimately contain "投标" without
the document itself being a bid — verified against real 目录 marker
position, consistently ~211-221 chars in, in both real tender fixtures).

**Why the scanned-PDF path failed, not just underperformed**: both
inspected failures show the same reasoning pattern, not random noise. These
真实 documents' cover pages follow an industry convention — the bidder
reprints the tender issuer's own cover-page template (which legitimately
carries both "招标单位" and "投标单位" fields, plus the label "投标文件")
and fills in their own company name. The model, seeing both labels and the
tender issuer's name printed first, reasoned this must be "the tender's own
blank format specification for what a bid cover should look like" rather
than recognizing it as an actual completed, submitted bid — a plausible
sounding but wrong inference, and it happened on both inspected cases, not
once. A single page's visual content doesn't carry enough signal to resolve
this — it's not a prompt-wording problem.

**Also found, not the root cause**: `_mm_call` at `temperature=0` was not
fully deterministic in this test — the same document returned differently
shaped raw responses across calls, once triggering a JSON parse failure
("Extra data"). Worth hardening the parser's robustness independently, but
fixing it would not have changed the 0/7 result — both inspected failures
parsed fine and were still wrong.

**Starting ideas for a future revisit** (not attempted, no fake confidence
either way):
- Look past page 1 — a genuine completed bid has real pricing tables later
  in the document; a blank format-sample page doesn't. More cost (multi-page
  call) but resolves the exact ambiguity found here.
- Look for a company seal/signature/stamp image on the cover as a positive
  "this is a completed, submitted copy" signal, distinct from which company
  name is printed where.
- Simply accept lower confidence and route "uncertain"/low-confidence
  results to the same manual 2-choice fallback that §3's "方案 B" already
  described — a hybrid, not purely automated or purely manual.

## 4. Summary cards

**Content source**: template-compose from already-extracted structured
fields (`project_name`, `category`, `supplier_name`, `row_count`, cover
scalars) — **not** a fresh freeform LLM read of the raw document. This
matters for two reasons:

- Matches CLAUDE.md's "LLM explains deterministic results... may not
  fabricate evaluation facts" — the summary describes what was already
  extracted and validated by the existing pipeline, it doesn't re-derive
  facts from scratch.
- Cheaper and more reliable — reuses data already in hand instead of a
  second full-document read.

The LLM's job is narrow: turn `{project_name, category, row_count, ...}`
into one or two readable sentences. Prompt must constrain it to those given
facts only — no inference, no quality judgment ("这份文件看起来
完整/合格" is out of bounds — design/27's red line: system states facts,
never pushes judgment).

**Card layout** (per tender/bid slot):
```
┌─────────────────────────────────┐
│ [招标] 金桥地铁上盖 J9A-03         │  ← LLM-composed 1-2 line summary
│ 阀门类，92 项采购清单              │
├─────────────────────────────────┤
│ 采购清单 92 项                     │
└─────────────────────────────────┘

┌─────────────────────────────────┐
│ [投标] 上海绵存智能科技            │
│ 报价清单，132 行                   │
├─────────────────────────────────┤
│ 报价清单 132 行 · 报价总计 ¥XX,XXX │
│ 含 8 行待确认，未计入官方评估       │
└─────────────────────────────────┘
```

Click card → existing detail view (QuoteGrid for bid cards, read-only list
view for the tender card) — D2, unchanged from design/27.

## 5. 报价总计 — D3 basis and labeling

Sum of **all** rows (`effective_total_price` where present, per the
price-basis bridge already built), including pending/REVIEW rows — per D3.
**Must** carry a visible "含 N 行待确认，未计入官方评估" label whenever any
row is not yet AUTO-confirmed, so this number is never mistaken for the
official bid-matrix total (CLAUDE.md: "pages, exports, evaluation
explanations must consume the same business-service result" — this label is
what keeps a second, rough number from silently disagreeing with the first).

## 6. Upload zone consolidation

Three precise cards (上传招标文件/上传采购清单 Excel/拖入所有投标文件)
retired once §3's scanned-PDF path is measured and working — not before,
to avoid a regression window where scanned bids can't be classified at all.
The unified zone's existing Excel handling (already reliable, cut 5) and
native-PDF handling (§3, cheap reuse) don't need to wait; only the
scanned-PDF cutover is gated on the accuracy measurement.

## 7. Delivery plan

| Cut | Content |
|---|---|
| 1 | Tier 1.5 scanned-PDF classifier (page-1-only call) + accuracy measurement against real corpus |
| 2 | Native-PDF cheap classification (reuse text-layer extraction + Tier 0-style heuristic) |
| 3 | Wire both into the unified drop zone's dispatch logic; retire the three precise cards once §6's gate passes |
| 4 | Summary-card component (template-compose + narrow LLM call) + card layout, replaces default landing view |
| 5 | 采购清单数量/报价清单数量/报价总计 stats row, D3 labeling |

## 8. Out of scope

- Any change to the QuoteGrid review surface itself (D2 — stays as-is,
  reachable via drill-down).
- Any change to gate semantics, evaluation totals, or the bid-matrix
  computation (§5's total is a separate, clearly-labeled rough number).
- PDF without any embedded list — still an untested path per design/28 §9,
  unchanged by this round.
