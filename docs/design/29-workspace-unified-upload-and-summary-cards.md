# 29 — Unified Upload Zone + Summary Cards (workspace redesign, round 2)

> **Status — all cuts delivered 2026-08-20.** Follows design/27 (workspace
> shell) and design/28 (Tier 0/1 classification). Extends both rather than
> replacing them — the classification ladder and the QuoteGrid review
> surface stay.
>
> **Cut 1 (scanned-PDF Tier 1.5) — three findings in sequence, latest one
> is current**:
> 1. First measurement against the real corpus per §3's own requirement:
>    **0/7** on real scanned bid PDFs (page-1-thumbnail-only input), not
>    random noise — both inspected failures showed the same systematic
>    misreading (§3.1). Decision at the time: pause automated scanned-PDF
>    classification, keep the three precise cards as the PDF entry point.
> 2. User then asked explicitly for a popup fallback ("招标文件、投标文件
>    的PDF识别给出弹出问询"), superseding the pause: native PDFs route
>    automatically off the real keyword judge; scanned/uncertain PDFs
>    trigger a two-choice popup instead of guessing or falling back to a
>    visible card.
> 3. **2026-08-21, user pushed back on accepting 0/7 as final** ("是不是
>    没有利用好LLM" — asking whether the LLM was under-used, not whether
>    it's capable). Correct call: the 0/7 test sent only page 1 as a
>    low-res thumbnail — a genuinely minimal input, not "the LLM tried and
>    failed." Re-tested with 3 pages at native resolution + a prompt that
>    names the exact failure mode (bidders reprint the tender issuer's own
>    cover template, which legitimately carries both "招标单位" and
>    "投标单位" fields) — same corpus, same flash-tier model, **8/8**. See
>    §3.1's updated result. Scanned-PDF auto-classification is **live**,
>    not paused — the popup now fires only on genuine `uncertain`, matching
>    what §1 originally asked for without the fallback carrying the whole
>    load.
>
> The three precise cards (design/28 cut 5) are **not deleted** — `IntakeUploader`
> stays mounted (needed for its `handleFile` capability, now called
> programmatically for auto-routed and popup-routed tender files) and all
> three cards remain reachable via a "看不到卡片？点这里手动选择上传区域"
> escape hatch, shown by default whenever there's real content to display
> and automatically if the classify call itself fails outright. This isn't
> a compromise on §1's ask — it's the same "no dead-end" principle used
> throughout design/27, applied to the one case (classify API itself down)
> that a popup can't route around.
>
> Cuts 4-5 (summary cards + stats) shipped as designed, with one scope
> narrowing recorded honestly: cards sit **above** the existing tabs rather
> than replacing the default view outright, and clicking a card switches
> the active tab instead of navigating to a separate drill-down route. This
> preserves 100% of the QuoteGrid review surface (D2) while delivering the
> actual ask (overview first, detail on demand) with much less structural
> risk than a full landing-page rebuild — a deliberate, smaller-blast-radius
> substitution made while implementing solo and unsupervised, not a silent
> downgrade.

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

### 3.1 Measured result — first 0/7 (2026-08-20), then 8/8 after fixing the actual problem (2026-08-21)

**First measurement (2026-08-20), page-1-thumbnail-only input**: ran
`classify_document_kind` against all 7 real scanned bid PDFs in the corpus
(泰科龙/绵存/凯硕 + prj1 浦东/亨通/宏胜/远东). **0/7 correct.** Native-PDF
path (`classify_native_pdf`, zero-model-call keyword judge) worked correctly
on both real native tender PDFs once fixed to scan only the cover region
before the table-of-contents marker (an earlier version scanning the whole
first 2 pages hit both tender/bid keyword sets — tender documents' own
table of contents lists chapters like "第四章 投标须知", which legitimately
contain "投标" without the document itself being a bid — verified against
real 目录 marker position, consistently ~211-221 chars in, in both real
tender fixtures).

**Why the scanned-PDF path failed, not just underperformed**: both
inspected failures showed the same reasoning pattern, not random noise.
These 真实 documents' cover pages follow an industry convention — the
bidder reprints the tender issuer's own cover-page template (which
legitimately carries both "招标单位" and "投标单位" fields, plus the label
"投标文件") and fills in their own company name. The model, seeing both
labels and the tender issuer's name printed first, reasoned this must be
"the tender's own blank format specification for what a bid cover should
look like" rather than recognizing it as an actual completed, submitted
bid — a plausible sounding but wrong inference, and it happened on both
inspected cases, not once.

**2026-08-21 — user pushed back rather than accepting 0/7 as final**:
asked directly whether the LLM was under-used, not whether it was
fundamentally incapable. Correct call — the 0/7 test sent exactly **one
page**, compressed to a **2-megapixel thumbnail**, in a **single shot**,
which is a minimal/cheap probe, not "the model tried its best and failed."
Re-tested with the two changes the failure analysis itself pointed at
(first two "starting ideas" below): **3 pages at native render resolution
(6-megapixel budget, no thumbnail compression) + a prompt that names the
exact failure mode found above** (explicitly: seeing "招标单位" printed
does not by itself mean tender — check whether "投标单位" is filled with a
real company name, and whether later pages carry 投标函/授权书 content that
only exists in submitted bids). Same corpus, same `qwen3-vl-flash` tier
(no model upgrade) — **8/8** (7 bids + 1 tender, all correct). Verified at
two levels: a standalone repro script, and — separately — the actual
production call path (`scanned_pdf_classify.classify_pdf_for_dispatch`)
against the same files, both giving 8/8. Permanent regression coverage:
`test_scanned_pdf_classify.py::TestScannedPdfRealAccuracy`
(`@pytest.mark.e2e`, needs a real `DASHSCOPE_API_KEY`, skipped by default
matching this project's fresh-E2E convention).

**Consequence**: scanned-PDF auto-classification is now **live** in
`classify_document_kind`/`classify_scanned_pdf`/the `/classify-tier0`
route — not paused, not routed unconditionally to the popup. The popup
(§3, "方案 B") still exists and still fires on genuine `uncertain`
verdicts (including when no vision client is configured at all), which is
exactly the "honest can't-decide path" §1 asked for — it's no longer
carrying 100% of scanned-PDF traffic by design.

**Also found along the way, not the root cause of either result**:
`_mm_call` at `temperature=0` was not fully deterministic in the first
test — the same document returned differently shaped raw responses across
calls, once triggering a JSON parse failure ("Extra data"). Did not recur
in the 8/8 re-test; still worth hardening the parser's robustness
independently at some point, but it did not explain either the 0/7 or the
8/8 result — the first test's inspected failures parsed fine and were
still wrong on content, not on format.

**Remaining idea not yet needed**: looking for a company seal/signature/
stamp image on the cover as a positive "this is a completed, submitted
copy" signal was on the original list of things to try — turned out
unnecessary once the template-reprint ambiguity was named explicitly in
the prompt and later pages were in view. Not implemented; note kept in
case a future corpus reintroduces cases the current approach still misses.

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

Click card → switches the active tab to that card's QuoteGrid (bid) or the
list tab (tender) — D2, unchanged from design/27. Delivered as "cards sit
above the existing tabs, click switches tab" rather than a separate
drill-down route (see status banner's scope-narrowing note) — same
functional outcome (overview → full detail on demand), lower structural
risk.

## 5. 报价总计 — D3 basis and labeling

Sum of **all** rows (`effective_total_price` where present, per the
price-basis bridge already built), including pending/REVIEW rows — per D3.
**Must** carry a visible "含 N 行待确认，未计入官方评估" label whenever any
row is not yet AUTO-confirmed, so this number is never mistaken for the
official bid-matrix total (CLAUDE.md: "pages, exports, evaluation
explanations must consume the same business-service result" — this label is
what keeps a second, rough number from silently disagreeing with the first).

## 6. Upload zone consolidation — done via popup, not accuracy-gated

Superseded by the user's explicit popup ask (see status banner). The three
precise cards are hidden by default (`v-show`, not deleted) rather than
formally "retired" — they reappear via a manual toggle link, or
automatically if the classify call itself errors out. This achieves the
same outcome (single visible entry point) the original accuracy-gated plan
was aiming for, without waiting on scanned-PDF classification accuracy that
turned out not to be achievable with a page-1-only vision call (§3.1).

## 7. Delivery plan — all cuts done 2026-08-20

| Cut | Content | Result |
|---|---|---|
| 1 | Tier 1.5 scanned-PDF classifier (page-1-only call) + accuracy measurement against real corpus | Done, measured 0/7, real bug found+fixed on the native path (§3.1), kept as infrastructure |
| 2 | Native-PDF cheap classification (reuse text-layer extraction + Tier 0-style heuristic) | Done, verified against real fixtures (`金桥`/`prj2` tender PDFs) |
| 3 | Wire into the unified drop zone's dispatch logic; two-choice popup for uncertain PDFs; three precise cards hidden by default, not deleted | Done — `classify-tier0` extended to return real tender/bid/uncertain for PDFs, `IntakeUploader.handleFile` exposed for programmatic routing, `Modal.confirm`-based popup wired |
| 4 | Summary-card component (template-compose + narrow LLM call) + card layout | Done — `POST /api/intake/summarize-facts` (reuses `paddle_doc_meta`'s existing text client, zero new provider code), cards sit above the tabs (scope-narrowed from "replaces landing view", see status banner) |
| 5 | 采购清单数量/报价清单数量/报价总计 stats row, D3 labeling | Done — `bidStatsFor()`, D3 basis (all rows incl. pending) with "含 N 行待确认，未计入官方评估" label |

## 8. Out of scope

- Any change to the QuoteGrid review surface itself (D2 — stays as-is,
  reachable via drill-down).
- Any change to gate semantics, evaluation totals, or the bid-matrix
  computation (§5's total is a separate, clearly-labeled rough number).
- PDF without any embedded list — still an untested path per design/28 §9,
  unchanged by this round.
