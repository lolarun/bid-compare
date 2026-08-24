# 41 — Evaluating qwen-vl-ocr as a Paddle replacement

> **Update 2026-08-24 — the page-filter half of this investigation shipped
> (behind a default-off switch); the engine-replacement half did not.**
>
> §1's question ("why can't Paddle be replaced") led to two separate tracks.
> The **cost-reduction track** (classify pages, send only quote pages to Paddle)
> is now implemented in `apps/api/intelligence/page_filter.py` and wired into
> `pipeline.py` — **off unless `MIMO_API_KEY` is set**. See HANDOFF's
> "design/41 分类筛页已接入" section for the three fail-safe defenses, the
> measured run-to-run variance that motivated the union-of-rounds design, and
> the honest speed tradeoff (79% cheaper, 33% slower end to end).
>
> The **engine-replacement track** below (does qwen-vl-ocr match Paddle) is
> still unbuilt. §2's one-page sample stands; §5's methodology was never run.
> Additional evidence gathered since: qwen-vl-ocr returns HTML whose header row
> is missing a column, and repeated calls on the same page gave different wrong
> values (`228.04` — traced to being the *neighbouring row's tax amount*, a real
> value bound to the wrong row/column). `qwen3.5-ocr` was worse: header split
> into fragments, whole table shifted by one row. Neither is close to Paddle's
> coordinate-grid output. **Recommendation: do not pursue engine replacement
> without a much stronger reason than cost** — the cost problem now has a
> cheaper answer that does not touch the recognition engine at all.

> **Status: proposal, nothing built.** §2 is a single measured data point (1 page,
> 5 rows) that motivated this document, not a corpus result. §3–§5 lay out the
> methodology to run before any accuracy claim is made. No code changes; this
> is the design-first step CLAUDE.md §6 requires before implementation.

## 1. Trigger

User, 2026-08-24, after a cost-reduction investigation (page-classify-then-Paddle,
recorded in HANDOFF) kept running into vendor-side content-safety refusals on
both qwen-vl-plus and ernie-4.5-turbo-vl:

> 我不理解为什么 Paddle 的分类识别无法用其他模型替代

Answering that honestly required admitting a gap: `docs/design/26` benchmarked
Paddle against `qwen3.7-plus` (a general chat/VL model, prompted into emitting
CSV) — never against `qwen-vl-ocr-latest`, DashScope's **purpose-built OCR
model** (`ocr_options={"task":"table_parsing"}`, already wired in
`DashScopeOCRProvider._ocr_page`, unused since the codebase's earliest
"Phase 1" prototype, commit `0852f9b`, which tested it on 1–2 documents at a
≥95% threshold — not a corpus benchmark and not a Paddle comparison).

> 认真启动"换引擎"这个更大的调研

This document is that investigation's plan, not its result.

## 2. What is already measured — one page, not a verdict

`provider._ocr_page(png)` on 泰科龙 page 5 (5 rows, all in the golden corpus),
scored against the real Excel truth (`read_reference`):

| spec | golden unit_price | got | golden total | got |
|---|---|---|---|---|
| DN20 | 69.12 | 69.12 ✓ | 69.12 | **228.04 ✗** |
| DN25 | 103.19 | ✓ | 1754.16 | ✓ |
| DN32 | 154.78 | ✓ | 309.56 | ✓ |
| DN40 | 203.45 | ✓ | 203.45 | ✓ |
| DN50 | 275.49 | ✓ | 4683.27 | ✓ |

unit_price 5/5, total_price 4/5. The one miss is on a `qty=1` row (where
unit_price should equal total_price) — the same ambiguity class Paddle hits
(design/33's `_classify_trailing_cells` docstring), but the wrong value here
(228.04) doesn't trace back to anything else in the row — looks like a
genuine misread, not a picked-the-wrong-duplicate error.

**A structural defect, independent of value accuracy**: the returned HTML
header row has 14 `<td>`, every data row has 15. Diffing against the header
text against the real document (checked via `pdfplumber` on the sibling
tender PDF that embeds the same table) shows the header is missing a column
(`专业` — the row's 2nd cell, "给排水", has no header to attach to). A naive
"map data column N to header column N" parser would misalign every field
after that point. This is not a values problem; it's a parser-design
constraint the extraction code has to handle before field accuracy means
anything.

**Sample size: 1 page, 5 rows, 1 document.** Nothing here should be read as
"qwen-vl-ocr is roughly as good as Paddle" or "qwen-vl-ocr has a header bug" —
both need the full corpus to say responsibly.

## 3. Why this needs its own extractor, not a one-line adapter swap

Paddle's P1 adapter (design/26 §5) worked because Paddle's native output is
already a coordinate grid (`cells`/`matrix`) — serializing that into canonical
CSV and feeding the existing `build_draft()` was mechanical. `qwen-vl-ocr`'s
`table_parsing` task returns **HTML tables**, a fundamentally different shape:
row/column identity has to be *parsed*, not read off a coordinate.

What can genuinely be reused, not rebuilt:

- **Column → role mapping**: `intelligence/column_roles.py` (design/40) already
  solves "given a header row + sample rows, which column is qty/unit_price/
  total_price" via keyword table → deterministic arithmetic verification →
  model fallback. It operates on `header: list[str]` + `rows: list[list[str]]`
  — exactly what an HTML table parses into. This document's extractor should
  call into it, not reimplement column detection.
- **Golden reading and row-matching**: `test_scenarios_e2e.py`'s
  `read_reference()`, and its row-matching conventions, are the same golden
  Paddle was scored against. Reusing them (not writing a parallel scorer) is
  what makes the resulting numbers *comparable* to design/26 §6's table rather
  than a new, incompatible metric.

What's genuinely new work:

- An HTML→grid parser (`<tr>`/`<td>` → `list[list[str]]` per page).
- A policy for the header/data column-count mismatch found in §2 — options:
  (a) always trust `column_roles.resolve_columns`'s arithmetic-verified
  mapping over positional header alignment when the counts disagree, since
  the whole point of that module is not depending on position; (b) detect the
  missing-header case specifically and re-derive it (e.g. widen the header
  using the first data row's known-safe columns). (a) is cheaper and reuses
  more; recommended default, but this is one of §5's open decisions.
- Cross-page row assembly (concatenating each page's rows into one document,
  parallel to what `build_draft` does for the Paddle/VL-direct CSV paths).

## 4. Cost

Real, not modeled: page 5 above cost 5771 tokens (`resp.usage.total_tokens`).
At the published rate (¥0.3/¥0.5 per million input/output tokens) that's
roughly ¥0.003/page — the same order of magnitude as qwen-vl-plus's
classification cost, **~30× cheaper than Paddle's ¥0.09/page**. Running the
full 159-page corpus (all 7 bid PDFs) once is on the order of ¥0.5 — the API
spend for this evaluation is not a real constraint.

**The cost that matters here is engineering time**, not API dollars: the HTML
parser, the header-mismatch policy, and wiring into the existing scorer.

## 5. Proposed methodology — mirrors design/26 §6, not a new bar

Reusing the acceptance dimensions Paddle was held to, so the result is a
direct comparison, not two different measurements that happen to both be
called "accuracy":

| Dimension | Paddle's recorded number (design/26 §6) | This investigation |
|---|---|---|
| Row recall / precision | 95.1–100% across 7 docs | same corpus, same metric |
| qty / unit_price / total_price | 90.6% / 94.4–96.0% | same fields, same golden |
| name | 95.3% | same |
| spec | 84.1% | same |
| Mixed orientation (宏胜) | 98.5% / 97.8%, no pre-check | same document |
| Duplicate copies (上海浦东) | 97.8% / 99.2% | same document |
| Timing | 17–85s/doc (Paddle) | record for comparison, not a bar — a 9-20x
  slower result was Paddle's own reason for existing (§1 trigger) |

**Non-goal, stated explicitly**: this document does not decide whether to
replace Paddle. Per this project's design-first convention, a result — good
or bad — gets written up and put to the user before anything changes in
production. Today's routing (`.claude/rules/recognition.md`: Paddle is the
sole visual engine for scanned PDFs) does not change until a decision is made
on the *result* of this evaluation, not on this plan.

## 6. Open decisions

1. **Header/data mismatch policy** (§3): trust `column_roles`'s arithmetic-
   verified mapping over position (recommended), or detect-and-repair the
   header. Affects how much of the existing design/40 infrastructure gets
   reused vs. how much new repair logic gets written.
2. **Scope**: all 7 documents from the start (recommended — cost is
   negligible per §4, and a partial-corpus result isn't comparable to
   design/26's numbers), or a 2-document pilot first to validate the parser
   before spending engineering time on all 7.
3. **Where the extractor lives**: a standalone `scripts/` evaluation script
   (matching this session's `try_page_classify_gate.py` precedent — read-only
   against fixtures, writes to `outputs/`, no production code touched) until
   a decision is made, consistent with how design/33 and today's page-classify
   work were both prototyped before touching `apps/`.

## 7. Out of scope

- Any change to production routing or `apps/api/intelligence/*` — this is
  measurement only.
- The page-classify-then-Paddle cost-reduction track (HANDOFF's "Paddle 页面
  分类降本实验" section) — a separate, already-in-progress investigation with
  its own open failure modes. This document does not depend on it and does
  not block on it.
