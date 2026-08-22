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

---

## 9. Round-3 UI feedback (2026-08-21) — one card per file

The user hand-tested the delivered round-2 UI and gave seven numbered
requirements. Six are UI changes (implemented, §10); the seventh is an
investigation (§11). Recorded verbatim in intent, not paraphrased away:

1. Dropping N files must show how many files are being processed — i.e. N
   cards appear below.
2. A card whose file type is not yet determined shows a **分析中** badge.
3. Card badges have exactly four categories: 招标文件 / 采购清单 /
   投标文件 / 报价清单.
4. After the badge, show the **unit name** — 招标单位 for a tender
   document, 投标单位 for a bid — in a larger font, and *only* the unit
   name.
5. There must be a tender/bid summary (LLM, as a by-product of
   classification); a quote total is desirable so the reader can
   cross-check it against the parsed detail below.
6. 采购清单 / 报价清单 counts must be stated in **项**, never **行**,
   whether they came from Excel or from a parsed PDF.
7. 凯硕新正 recognizes with no 单价 and no 合计 — an earlier test passed.

### Why round 2 did not already satisfy 1–4

Round 2 had no card *model*. Cards were three ad-hoc template branches:
the tender card existed only once `tenderResult` was non-null, the
采购清单 Excel had **no card at all** (it set `excelFile` and showed a
toast), and a file still being classified had nothing on screen — only a
"已上传 N 个文件" counter line that did not correspond to anything
visible. Badges covered two of the four categories, and the unit name was
buried inside the LLM summary sentence rather than being its own field.

## 10. Design: cards are a projection, not a fourth state machine

One dropped file = one card, from the moment it lands to the moment
recognition finishes. The badge changes (分析中 → one of the four
categories); the card itself never disappears and is never re-created.

**Cards hold no state of their own.** `utils/docCards.ts` projects four
existing state sources into a `DocCard[]`:

| Source | Owns | Produces |
|---|---|---|
| `pendingClassify` (new, WorkspaceView) | files between drop and verdict | `analyzing` cards |
| `IntakeUploader` (tender) via `@progress`/`@extracted` | tender upload + polling | the `tender` card |
| Excel preview (`uploadExcel`) | 采购清单 preview | the `tender_list` card |
| `batchFiles` (`useSupplierUpload`) | bid upload/recognition/confirm | `bid` / `bid_list` cards |

Keeping a separate card state would guarantee drift between what the card
says and what the pipeline is actually doing — which is the exact class of
bug this round is fixing. The projection functions are pure and unit-tested
(`__tests__/docCards.test.ts`), so each of req1–req6 has a direct assertion
instead of resting on a manual-test retelling.

Three consequences worth stating explicitly:

- **req3 needs a label on the bid entry.** Excel/CSV 报价清单 and PDF
  投标文件 go through the *same* upload/recognition/confirm pipeline —
  the distinction is presentational only, so `BatchFileEntry` carries a
  `docKind` tag (from the classify verdict, falling back to the file
  extension) rather than forking the pipeline.
- **req4 needed a new extracted field.** The tender side had no
  "who issued this document" scalar at all — only project name/code/dates.
  `tenderer` is now part of `vl_tender._META_KEYS` and both cover-scalar
  prompts (vision and Paddle text-layer). Project name is *not* used as a
  stand-in: a project name is not a unit name, and substituting it would
  make the card state something the document never said (design/27 red
  line 1). "Still recognizing" and "recognized, but the document does not
  say" are rendered differently, never collapsed.
- **req5 keeps two totals apart.** 明细合计 (sum of the extracted rows)
  and 文件声明总价 (`_doc_meta.bid_total`, what the document itself
  declares) are separate facts on the card and separate lines in the
  summary facts. They are never merged into one "总价": a disagreement
  between them is precisely the signal a human is being asked to check
  (same reasoning as the checksum gate).

## 11. req7 — 凯硕新正 missing 单价/合计

Investigation, not a UI change. **Nothing was lost in recognition — the grid
reads the wrong key.**

### 11.1 Measured (Paddle snapshot replay, `quote_kaishuo.json`, zero API cost)

Detail rows recognized from `金桥地体上盖项目-凯硕新正投标文件.pdf`: 90.

| field | 凯硕 filled | 绵存 filled |
|---|---|---|
| `unit_price` (generic) | **0 / 90** | 87 / 87 |
| `total_price` (generic) | **0 / 90** | 86 / 87 |
| `unit_price_excl_tax` | 89 / 90 | 0 / 87 |
| `total_price_excl_tax` | 89 / 90 | 0 / 87 |
| `unit_price_incl_tax` | 82 / 90 | 0 / 87 |
| `total_price_incl_tax` | 87 / 90 | 0 / 87 |
| `tax_rate` / `tax_amount` | 89 / 90 | 0 / 87 |

凯硕's quote table labels **every** price column with 含税/不含税
(`单价(不含税)` / `合计(不含税)` / `税率` / `税额` / `单价(含税)` /
`合价(含税)`). `vl_quote._SLOTS` routes those into the tax-qualified slots
and the generic `unit_price`/`total_price` slots deliberately reject
tax-labelled columns — that rejection *is* the A2 fix (a generic slot
swallowing 含税单价 made `derive_price_basis` report `excl_tax` and
undercharged the whole comparison by one tax rate). So `unit_price = None`
on 凯硕 is the correct, intended output; 绵存's plain `单价`/`合价` table is
the case where the generic slots are the right home.

`WorkspaceView.gridColumns` renders exactly two price columns, keyed
`unit_price` and `total_price`. For any supplier that separates tax basis,
both render empty.

**Why the earlier test still passed:** `test_paddle_quote_api_e2e` asserts
row counts and a successful confirm, and its review-row resolver accepts
`total_price` **or** `total_price_incl_tax` **or** `total_price_excl_tax`.
No test ever asserted which key the grid displays. Both statements are
true at once: recognition passes, display is blank.

**Fix (not yet applied — needs a decision):** the grid should render the
口径-resolved price. `pipeline._postprocess_quote` already attaches
`price_basis` / `effective_unit_price` / `effective_total_price` per row
(凯硕 row 1 → `dual`, 71.0, 71.0). Options: (a) render
`effective_unit_price`/`effective_total_price` with the basis shown as a
column caption, or (b) render 含税/不含税 as separate column pairs when
`price_basis === 'dual'`. (a) is one column set for every document shape;
(b) shows more but makes the column count document-dependent. Either way
the raw values stay untouched — this is a display mapping, not a data
change.

**Second, separate gap found while measuring:** the Excel/CSV quote path
(`tabular_ingestion._TABULAR_COLUMN_PATTERNS`) is a *different* column
table from `vl_quote._SLOTS` and has no `tax_rate` / `tax_amount` /
`total_price_excl_tax` roles at all. Running 凯硕's own `.xlsx` through it
drops 税率/税额/合计(不含税); 泰科龙's `.xlsx` comes out with
`unit_price = None` because `单价(不含税)` is claimed by the excl slot and
the file has no other 单价 column. Same supplier, PDF vs Excel, different
field set. Tracked in design/30.

---

## 12. Round-4 feedback (2026-08-21) — remove the manual upload cards

Hand-test of §10. Two reports:

> 左上角的这个没有必要，后面的两个也没有必要，容易造成困惑

The three manual-upload surfaces (`IntakeUploader`'s own dragger + progress
block, 上传采购清单 Excel, 拖入所有投标文件) are now a **second entry point
for the same thing**. Once every dropped file gets a card that shows its
own badge and progress, the tender uploader draws the same progress twice
and the two Excel/bid draggers duplicate the unified drop zone. All three
are removed.

`IntakeUploader` is still mounted (in a `display:none` wrapper) — it is the
only holder of the tender upload/poll/retry logic and the unified drop zone
calls it through a ref. What is gone is its *visible* UI, not the component.
The 招标三产物明细 card (采购清单 / 封面信息 / 品牌要求) stays: it carries
real content and is not an upload entry.

Consequence: the "classification failed → show the manual cards" fallback no
longer has anywhere to point. It now falls through to the **same two-choice
popup** as "verdict uncertain", so the file keeps moving instead of dying on
an error card. `showManualCards` is deleted.

### 12.1 The 泰科龙 "分类接口异常" — a client timeout, not a backend error

> 金桥地体上盖项目-泰科龙投标文件的异常是什么情况

Measured, not inferred:

- Backend log for that session: **six** `POST /api/intake/classify-tier0
  → 200 OK`, and **zero** errors on that route. The server answered every
  call successfully.
- Real classification cost, one file at a time (production
  `classify_pdf_for_dispatch` + real vision call): 泰科龙 **6.5 s**,
  凯硕新正 **8.9 s**, 上海绵存 **6.8 s** — all three `verdict = bid`,
  correct.
- Client timeout was **30 s** (`api/index.ts`).

All four files were dropped at once and `onDropAnyFiles` fired one classify
request per file with no concurrency cap. Scanned-PDF classification renders
the first `SCANNED_CLASSIFY_PAGES` pages, and every pdfium render entry point
must serialize through `document_loader._PDF_LOCK`
(`.claude/rules/recognition.md`) — while the tender PDF's own recognition was
holding that lock. So the requests queued server-side; 泰科龙 was last in
line and the browser gave up at 30 s while the backend was still working.
The card then said "分类接口异常", which was wrong twice over: the interface
did not error, and the file was not broken.

Two changes:

1. **Client-side serialization.** Classify requests are queued and sent one
   at a time. Firing N at once cannot go faster than the render lock allows;
   its only effect is to push every request toward the timeout together.
   Queued files say 排队中 on their card instead of pretending to be 判定中.
2. **Timeout 30 s → 90 s**, as headroom, not as the fix. A timeout is now
   also reported honestly ("自动判定超时") and routes into the two-choice
   popup rather than a dead end.

### 12.2 Separate defect observed in the same logs (not fixed)

`PUT /api/projects/{id}` returned 500 twice:
`sqlite3.IntegrityError: UNIQUE constraint failed: projects.name,
projects.code` from `routes/projects.py:73`. Cause: two workspaces
(`id=101`, `id=102`) both auto-filled their name from the same recognized
tender, and `persistProjectMeta()` writes it straight through — the second
one collides with `uq_project_name_code`. Uncaught, so it surfaces as a 500
rather than a usable message. Out of scope for this round; recorded so it is
not rediscovered as new.

---

## 13. Round-5 feedback (2026-08-21)

Three items, all from hand-testing §12.

### 13.1 招标解析信息 card removed too

> 招标文件解析信息我也不想要了

The 招标三产物明细 card (采购清单 N 项 / 封面信息 / 品牌要求) is gone as
well, so the `materials-strip` region no longer exists in any form. The
招标 summary card already states 采购清单 N 项 and the recognized 招标单位;
the extra card restated it in a second visual language right above.

Consequence: there is no longer a "重新上传" button for the tender. Replacing
it is done the same way as uploading it — drop the new tender PDF into the
unified zone; `routeToTender` calls the same `IntakeUploader` and
`onTenderExtracted` overwrites `tenderResult`. One entry point, same
reasoning as §12. `routeToTender` now also clears `tenderError` so a stale
failure does not ride along on the new card.

### 13.2 Classification concurrency: 1 → 4 (measured, not guessed)

> 现在对上传文件的解析式同步进行的么…可以将同步处理的任务数增大么

§12.1 serialized classification to stop the 30 s timeout. That over-corrected.
Splitting the per-file cost:

| file | pages | pdfium render of first 3 pages (holds `_PDF_LOCK`) | total classify |
|---|---|---|---|
| 泰科龙 | 53 | 1.64 s | 6.5 s |
| 凯硕新正 | 19 | 1.18 s | 8.9 s |
| 上海绵存 | 31 | 1.36 s | 6.8 s |

Only ~1.2–1.6 s of each call is the serialized render; the remaining 5–7 s is
the vision call, which parallelizes freely. So `CLASSIFY_CONCURRENCY = 4`:
worst case ≈ 4 × 1.5 s of render queueing + one vision call ≈ 12 s, far under
the 90 s client timeout, and four files finish in ~13 s instead of ~30 s
serial. The cap exists because the render segment genuinely cannot
parallelize — queueing more requests only moves the wait server-side. Files
beyond the cap say 排队中 on their card rather than pretending to be 判定中.

Note this is the *classification* stage only. Recognition already runs on the
process-wide `EXTRACTION_THREAD_POOL_SIZE` pool (default 8, `core/runtime.py`)
and was never the bottleneck being described.

### 13.3 Classification toast: one line, evidence moved to the badge

> 不需要这么长的 Message，直接提示：已完成某文件初步解析....识别为..... 即可

The toast was `「file」识别为投标文件（<the model's full multi-clause
reason>）` — a screenful nobody finishes reading. Now:
`已完成「file」初步解析，识别为投标文件`.

The reason is **not discarded** — it is stored per filename and rendered as
the card badge's hover tooltip. Deleting it would leave a verdict with no
stated basis, which design/27 red line 1 does not allow; burying it in a
toast that scrolls away was the actual problem.

### 13.4 明细合计 ¥0 on tax-split quotes — fixed as part of req5

Visible in the same screenshot: 凯硕新正 and 泰科龙 cards read
`明细合计 ¥0 · 文件声明总价 ¥932,154`. Same root cause as §11.1 —
`bidStatsFor` summed `total_price`, which is correctly empty when the
document splits 含税/不含税. It now sums `effective_total_price ?? total_price`
(the 口径-resolved value `derive_price_basis` already produces per row). This
changes which key the card reads; it does not touch any stored value and does
no cross-basis conversion. The grid columns themselves are still the open
decision in §11.1.

---

## 14. Round-6 (2026-08-21) — classify concurrency = 8, and why not unbounded

> 是否可以改为上传多少文件，即有多少个并行

Raised 4 → **8**. For the everyday case (dropping 4–8 files) that *is*
"one parallel slot per file". A cap still exists, for two reasons that are
measured rather than assumed:

1. **Downstream is 8 anyway.** Classification hands straight off to
   recognition, which runs on the process-wide pool
   `EXTRACTION_THREAD_POOL_SIZE` (default 8, `core/runtime.py:49`).
   Classifying 20 files at once only makes 20 jobs queue on 8 threads.
2. **The server-side rate guard does not actually bind on this path.**
   `DashScopeOCRProvider` keeps a per-API-key `Semaphore(_PER_KEY_CONCURRENCY)`
   (default 6) — but it is an **instance** attribute, and
   `routes/intake.py::classify_tier0_upload` calls
   `get_scanned_classify_call()` per request, constructing a **fresh
   provider, with a fresh semaphore, every time**. So concurrent classify
   requests never contend on it. Until that is fixed, the client-side cap is
   the only thing standing between "drop 30 files" and 30 simultaneous
   vision calls hitting provider rate limits — and a 429 storm is a worse
   user experience than waiting, because it surfaces as failed
   classifications rather than as a queue.

The tail cost stays modest either way: concurrency N ≈ N × 1.5 s of
serialized pdfium rendering + one vision call, so 8 files ≈ 12 s + 7 s ≈ 19 s,
well under the 90 s client timeout.

### 14.1 Defect recorded, not fixed

The per-key semaphore being per-instance (above) makes
`OCR_PER_KEY_CONCURRENCY` a no-op for `classify-tier0`, and for any other
path that builds a provider per request. Fixing it means caching the
provider (or hoisting the semaphore to module scope keyed by API key), which
touches provider construction shared with the recognition path — out of
scope for this round, recorded so it is not rediscovered as new.


## 15. Defect — duplicate project name from tender back-fill (fixed 2026-08-21)

Found in real server logs, not in a test. Two workspaces (project 101 and
102) each recognized the **same** tender document; `onTenderDone()` back-fills
`projectName` from the recognition result and `persistProjectMeta()` writes it
straight through with `PUT /api/projects/{id}`. The second write collided with
`uq_project_name_code` on `projects (name, code)` and raised an uncaught
`sqlalchemy.exc.IntegrityError`, which surfaced as **500 with a raw stack
trace**. `create_project` already handled the same constraint; `update_project`
did not.

This is an ordinary user path, not misuse: re-running a comparison, or a second
attempt after a failed upload, both recognize the same tender twice.

**Decision: fail loudly with a 409, do not auto-disambiguate.** The project
name is user-facing identity — silently rewriting it to "…(2)" would make two
workspaces for the same tender indistinguishable in the project list, and the
back-fill's whole point is that the recognized name is the *right* name.
Auto-fill-only-when-placeholder was rejected for the same reason: it would keep
the placeholder `新比价项目-<timestamp>` on the second workspace, which is
strictly worse than telling the user to pick a name.

| Layer | Change |
|---|---|
| `apps/api/routes/projects.py` | `update_project` catches `IntegrityError` → 409 with a readable detail; `create_project` reuses the same `_duplicate_project_409()` helper so both paths share one message |
| `apps/www/.../WorkspaceView.vue` | `persistProjectMeta()` catches the failure and shows it: a **persistent** inline error under the name input (a toast is not enough — the input still displays the unsaved value, so without a standing marker "not saved" is invisible until reload), plus a toast on the auto-fill path where the user isn't looking at the field. Per-keystroke manual edits get the inline marker only, no toast spam. |
| `apps/api/tests/test_project_routes.py` | New: 409 on create and update, row unchanged after rollback, session still usable, and same-name-on-same-row is not a collision |

Not changed: `views/projects/IndexView.vue` already surfaces `detail` from the
response, so the new 409 reaches the user there without edits.

## 16. 「轮询超时」不是超时（2026-08-21 手测）

**现象**：四份文件同拖，上海绵存（31 页扫描件）的卡片显示"轮询超时"，
另外三份正常。

**测量**：服务端日志里 `GET /api/intake/jobs/{id}` **每一次都返回 200**，
零错误。失败发生在客户端。

**根因**：两层判断都错了。

1. axios 全局超时 15s，`getJob` 没有覆盖。多份扫描件同时识别时，识别线程
   占着 GIL 和 pdfium 的 `_PDF_LOCK`，一次主键读也要排很久才轮得到 —— 请求
   最终被服务好了（日志里的 200），但客户端早已放弃。
2. 前端把"连续失败 5 次"当成终态失败。2 秒一次 → **10 秒就判死刑**，而且
   把"查不到状态"直接写成"识别失败"。识别任务当时还在正常跑。

第 2 条比第 1 条更糟：它让用户以为要重新上传，而重新上传会白花一次真实
OCR 的钱，还丢掉后台已经算出来的结果。

**修法**：

- `getJob` 单独给 60s 超时。它是一次主键读，超时给宽不花任何代价。
- 放弃判据从"失败次数"改成"连续失败时长"（3 分钟）。忍耐期内卡片如实显示
  "连接不稳定，重试中（已 N 秒）"，**不说"失败"** —— 两者对用户的含义完全
  不同（要不要重新上传）。
- 真的放弃后给「重试」按钮，且**重试是重新挂轮询，不是重新识别**：放弃的
  常见原因就是查询超时，那个 job 多半已经跑完了。服务端明确判 failed 的
  卡片不给这个按钮 —— 重试也是同样的结果，给个点了没用的按钮比不给更糟。

**没做的**：没有降低识别侧的并发或给 pdfium 锁减压。这次的表现是客户端误判，
不是服务端真的扛不住；真要动并发得先量清楚服务端在多大压力下开始退化，
现在没有那个数据。

## 17. Vision-call concurrency on the classify path (2026-08-20)

`DashScopeOCRProvider` bounds concurrent calls per API key with
`_PER_KEY_CONCURRENCY` (`OCR_PER_KEY_CONCURRENCY`, default 6). That semaphore
dict used to be an **instance** attribute, which made the bound ineffective
across the provider's two construction sites:

- `main.py::_build_pipeline` builds **one** long-lived provider at startup;
  extraction jobs share it through the `ThreadPoolExecutor`, so they do
  contend on its semaphore.
- `scanned_pdf_classify.get_scanned_classify_call()` builds a **new** provider
  on every `POST /api/intake/classify-tier0` request (§3), so each request got
  a fresh, full quota — never contending with the other classify requests, nor
  with the extraction pipeline using the same key.

**Fixed**: the semaphore registry is now module-scoped and keyed by api_key
(`_shared_key_semaphore` in `providers/dashscope_ocr.py`), so the limit is
process-wide per key regardless of how many provider instances exist. Key
rotation (`_next_key`) is unchanged, distinct keys keep independent quotas, and
`OCR_PER_KEY_CONCURRENCY` remains the tunable. Regression test:
`apps/api/tests/test_ocr_provider_concurrency.py` (asserts two separately
constructed providers with the same key share one gate, and that filling the
quota through one instance blocks the other).

**Not a client-side cap.** There is no frontend concurrency limit on this path
to relax: `WorkspaceView.onDropAnyFiles` calls `classifyAndRouteFile` per
dropped file without awaiting or pooling. Whether that should gain a cap is a
separate UI question, not something this backend fix enables or removes.

**Still open — the route blocks the event loop.** `routes/intake.py::classify_tier0_upload`
is `async def` but does all its real work synchronously (`classify_tier0`,
`classify_pdf_for_dispatch`, and the vision call inside it). FastAPI runs
`async def` endpoints on the event loop, so concurrent classify uploads
currently serialize at 1 in flight — and they stall every other request on the
loop while a vision call is outstanding. The per-key semaphore is therefore the
correct bound but not yet the *binding* one for this route. Moving the blocking
work off the loop (plain `def`, or `run_in_executor`) is the follow-up; it is
deliberately **not** bundled here, because it changes the latency profile of
the whole API surface and deserves its own measurement.

## 18. 全部超时的真因：阻塞调用写在 `async def` 路由里（2026-08-22）

§16 把「轮询超时」归因成「GIL 和 pdfium 锁竞争让请求排队」。**那个判断是错的
——现在撤回。** 锁竞争只会让请求变慢，不会让请求根本得不到处理；而实测现象
是**同一时刻所有请求一起超时**，包括跟识别毫不相干的 `PUT /api/projects`。

真因：FastAPI 对两种路由的调度完全不同——`def` 路由丢进线程池，`async def`
路由直接跑在事件循环上。四个路由把同步阻塞重活写在了 `async def` 里：

| 路由 | 阻塞的东西 |
|---|---|
| `intake.upload_document` | `create_job`：SHA256 整个文件 + 写盘 + 写 SQLite（抢写锁） |
| `intake.classify_tier0_upload` | 一次**真实视觉调用，实测 6.5-9 秒** + pdfium 渲染 |
| `intake.summarize_facts` | 纯文本 LLM 调用 |
| `analysis.tender_list_match` | `import_and_match`，可能走 embedding HTTP |

一次拖 4 个文件 = 4 次分类 × 约 7 秒，**服务器有近半分钟完全不响应任何请求**。
这一条解释了这轮全部四次超时，不需要四个不同的解释：

- 泰科龙分类「接口异常」（§14 归因为客户端 30s 太紧）
- 上海绵存「轮询超时」（§16 归因为 GIL/锁竞争）
- 泰科龙上传 `timeout of 60000ms exceeded`
- 同一时刻 `PUT /api/projects` 报「未保存成功」

前两条的**加大超时/延长忍耐期**是对症的，留着没坏处（网络本来也会抖），但
它们治的不是这个病。

### 改动

四个路由改成 `def`，FastAPI 放进线程池；`await file.read()` 相应改成
`file.file.read()`。加 `test_routes_not_async.py`：AST 扫描 `routes/*.py`，
`async def` 里出现已知阻塞调用即失败，另有 4 条按名字钉住这四个具体路由
（防重构改名导致扫描漏掉）。
