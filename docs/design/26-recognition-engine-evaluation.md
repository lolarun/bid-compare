# 26 — Recognition Engine Candidate Evaluation (Scanned Bid PDFs)

> **Status — proposed, 2026-08-13.** Covers the harder half of a
> "recognition engine replacement" investigation prompted by a customer
> complaint (slow recognition, WPS comparison) — the scanned bid/quote side.
> The easier, independent half (tender procurement lists with a text layer)
> is split out to `docs/design/25-tender-text-layer-extraction.md` per the
> user's explicit direction (2026-08-13): Track A ships first, on its own
> schedule, with zero coupling to this document's decisions.
>
> User has confirmed two of this document's open questions from the prior
> discussion round: Track A is independently prioritized (see above), and
> the Phase 2 pass-threshold matrix in §6 is accepted as proposed. Hengtong's
> 65.4% recall root cause (§6, row 6) remains open and is scoped as part of
> Phase 2's own evaluation work, not a prerequisite to starting it.
>
> Basis: CLAUDE.md §1/§4/§6, `.claude/rules/recognition.md`,
> `.claude/rules/tests.md`. Evidence: `scripts/{try_paddleocr_vl,
> score_paddleocr_vl,stitch_vs_multi_bench}.py` + `outputs/{baidu_paddleocr_vl,
> baidu_unlimited_ocr}/` (gitignored — rerun the scripts to reproduce) +
> `HANDOFF.md`'s 2026-08-13 merge-checkpoint section.

## 1. Why this is a evaluation project, not a swap

`vl_quote.py`'s bid/quote recognizer only talks to the rest of the pipeline
through one contract: `ExtractionDraft`, built by `build_draft()`. Alignment,
the quality gates, `copy_no` dedup, dry-run confirmation, and every design/24
frontend surface all consume that shape and nothing upstream of it. Swapping
the model or provider behind that boundary is architecturally invisible to
everything downstream — **provided** the new path produces the same shape
with the same honesty guarantees (no silent row drops, no silently-derived
amounts, REVIEW/BLOCKED gates fed real signal). That precondition is the
actual size of this project: not "call a different API," but "prove the new
path can honestly fill every field the current path's quality gates depend
on." This is why it's a full evaluation project and not a config flip.

## 2. Evidence gathered so far (2026-08-12/13 exploratory session)

All of the following is real, measured, and reproducible via the scripts
named in the header — not modeled or estimated.

### 2.1 Image-stitching is a dead end

Tested whether combining N page images into one composite image (instead of
sending N separate image parts in the VL-direct call, which is already a
single API call — see `.claude/rules/recognition.md`, "整份文档一次调用")
would save time. Result: yes, materially (467s → 188s on 凯硕新正/19 pages),
but only by degrading resolution enough to lose ~1/3 of rows (100% → 67.4%
recall). Attempts to preserve resolution by raising the pixel budget produced
base64 payloads (37-96MB) that failed to upload at all (`WinError 10053`
connection aborted / write-timeout, reproduced 3 times at different pixel
targets). **Conclusion: not a viable direction, no further investigation
planned.**

### 2.2 PaddleOCR-VL — candidate model

A 0.9B-parameter open-source vision-language model purpose-built for document
parsing (tables/formulas/seals/skew), self-hostable (PaddlePaddle/vLLM/
SGLang/llama.cpp/MLX) or available via Baidu Cloud's hosted
"文档解析（PaddleOCR-VL）" API (`aip.baidubce.com/.../paddle-vl-parser`,
same OAuth credentials already in `apps/api/.env` as the
`BAIDU_UNLIMITED_OCR_*` keys used for the 2026-07-13 prior experiment).
Published benchmarks: 96.3% OmniDocBench v1.6 overall, table TEDS 91.71/94.67,
cross-page table merging (v1.5+), 1.22 pages/sec at vLLM throughput.

**Measured on this project's own 7-document benchmark corpus** (row-level
sequence-presence recall — see §2.3 for what this metric does and does not
prove):

| Document | Pages | Time | Seq recall | Has 序号 column |
|---|---|---|---|---|
| 凯硕新正 | 19 | 34-57s | 100% (89/89) | Yes |
| 泰科龙 (hardest known case, transposed table) | 53 | 70-85s | 100% (89/89) | Yes |
| 远东 | 19 | 43s | 100% | Yes |
| 上海绵存 | 31 | 59s | 100% (89/89) | No — content-align verified |
| 上海浦东 | 15 | 45s | 100% (136/136) | No — content-align verified |
| 宏胜 | 11 | 20s | 100% (136/136) | No — content-align verified |
| 亨通 | 11 | 24s | **65.4%** | No — content-align verified |

vs. current VL-direct baseline: ~630-710s total (orientation pre-check +
extraction) for a comparably-sized document. **9-20x time advantage**, and
critically, **100% row coverage on 泰科龙**, the one document that has
defeated every fast-path attempt tried in this project's history to date
(this session's own stitching experiment, and the 2026-07-13 Baidu OCR
experiment which got only 59-70% on it).

### 2.3 What this metric does and does not prove

"Seq recall" checks whether a valid row-identity value (either a printed
序号 column, or — when absent — content-aligned name+spec+qty matching,
reusing `scripts/e2e_diff.py::diff_doc()`'s existing `content_align` mode)
was found for each golden row. It does **not** check that every field within
a found row is correct. An exploratory field-level scorer
(`scripts/score_paddleocr_vl.py`) was built and hit a real, diagnosed
limitation: some rows have empty cells that are not consistently represented
as empty placeholders in PaddleOCR-VL's `matrix`/`cells` output, causing
column-position drift that a fixed-header-index mapper cannot safely resolve
(verified concretely — see the row-1-vs-row-3 comparison in this session's
transcript, same table, same header, different effective column alignment).
**Field-level accuracy is not yet a trustworthy number for this candidate.**
Building a parser that handles this correctly is comparable in scope to work
already invested in this project's own quote-line validation logic
(`quote_fact.py`'s arithmetic checks) — real work, scoped into Phase 2 below,
not something to rush.

## 3. Architecture: how a new provider plugs in

No new abstraction is needed. `LLMProvider` (the existing interface
`DashScopeOCRProvider` implements) already defines the seam; a new provider
implements `vl_extract_csv` (or an equivalent structured-output method) and
`recognize_quote_vl`'s `vl_call`/`orient_call` injection points stay
unchanged. The two `hasattr(provider, "vl_extract_csv")` checks in
`pipeline.py` are defensive guards (per `.claude/rules/recognition.md`), not
a path-selection mechanism — a new provider either implements the interface
or the call fails loudly. No dual-path architecture is introduced by this
project; at any point in time exactly one provider is live per the existing
single-path rule, with `qwen VL-direct` and the candidate trading which one
that is across Phase 3/4 below.

## 4. Phase 0 — evidence infrastructure adoption (~half day)

1. Parameterize the two exploratory scripts (`try_paddleocr_vl.py`,
   `score_paddleocr_vl.py`) to `scripts/` conventions properly (they're
   already read-only/output-to-`outputs/`, but harden CLI args, remove any
   remaining hardcoded assumptions from the exploratory-script phase).
2. Bind `outputs/baidu_paddleocr_vl/` artifacts to code SHA / PDF SHA / model
   version in `docs/data/source_registry.json` or an equivalent manifest —
   this project has previously lost reproducibility on exploratory results
   that couldn't be traced back to a specific code state; don't repeat that
   here.

## 5. Phase 1 — (moved) — see `docs/design/25`

Tender-side text-layer extraction. Independent, ships on its own schedule,
no dependency on this document.

## 6. Phase 2 — candidate evaluation (design review before code lands)

**Core principle: evaluate the candidate through the production seam, not by
hardening the exploratory scorer.** The field-level scoring gap identified in
§2.3 gets solved once, correctly, as a `PaddleOCR-VL output → ExtractionDraft`
adapter (the `matrix`/`cells` structure is more regular than free-form CSV —
the empty-cell drift problem gets fixed once in the adapter, not worked
around per-script) — and then this project's **existing** `compute_quality`
+ the four quality gates + `scripts/e2e_diff.py` become the real scorer for
free. No new validation logic gets invented in a throwaway script.

Evaluation matrix (thresholds accepted by user, 2026-08-13):

| Dimension | Sample | Pass bar |
|---|---|---|
| Row recall / precision | 7 cable + valve documents | ≥ qwen baseline |
| Numeric triple + spec text | Same, vs golden | ≥ 96% (including spec — qwen's known weak point) |
| **Mixed orientation** | 宏胜 (180° mixed-orientation poison sample) | Passes without an external orientation pre-check, or Track B's time advantage is roughly halved |
| **Duplicate copies (正本/副本)** | 上海浦东 (272 = 136×2 raw rows) | Adapter must emit a copy signal feeding B0's dedup, or an equivalent structural duplicate-detection |
| **Run-to-run stability** | Each document, ≥3 runs | Amount-delta distribution, never a single-run number (HANDOFF §6 lesson 1 applies verbatim — qwen itself has 0.18% run-to-run variance and 3/10↔10/10 orientation collapse) |
| 亨通 65.4% | Dedicated root-cause | Determine whether the shortfall is the model, the exploratory scorer, or golden-side error — before this document can claim any accuracy number for 亨通 |

**Deployment form during evaluation**: cloud API (fast, zero ops). Note for
whoever makes the eventual production-hosting decision: **this machine is
Windows** — self-hosted vLLM most likely means WSL2 or a dedicated GPU box,
a real production cost that the "1.22 pages/sec" vLLM-throughput number does
not include. Don't let that number anchor the evaluation-phase deployment
choice.

## 7. Phase 3 — shadow running (after Phase 2 passes)

New provider runs behind `LLMProvider`, production traffic double-runs both
paths, records-only (no user-facing switch), reconciled via
`block_alignment`-style diffing between the two drafts — same pattern as the
existing Phase B-1 shadow precedent in this codebase. Re-calibrate
`INTEGRITY_*`/`SEQ_*` thresholds on the shadow data during this phase — those
thresholds were calibrated against qwen VL-direct's specific failure-mode
distribution (drop-rate/amount-defect correlation, truncation shape, seq
coverage distribution); a different model has a different failure
distribution and silently keeping qwen's calibration risks systematic false
pass/fail on the new path.

## 8. Phase 4 — cutover and qwen's downgraded role

Cutover criteria locked in this document once Phase 3 data exists (zero net
loss across N shadow documents + stability distribution not worse than qwen).
qwen VL-direct is **not deleted** — it downgrades to two roles: (a) fallback
when the candidate is BLOCKED or a document has no text layer, and (b) the
engine behind "targeted re-read" (a designed-but-unimplemented feature
already named in `HANDOFF.md`) — a 0.9B model is fast but has a real
capability ceiling; a slower, general VL model is the right tool for the
residual hard cases once the fast path handles the common case.

**Not separately funded**: orientation-precheck optimization. If Track B
succeeds, the whole 3-vote LLM-based orientation-detection cost disappears
(PaddleOCR-VL handles skew/rotation natively per §2.2). If Track B fails,
revisit optimizing it then — investing in it now is work that either gets
thrown away or was never needed.

## 9. Two carry-forward notes

1. **design/24 B2's "已转录 N 行" stage-progress text is qwen-streaming-
   specific.** A new provider's progress data source changes with it
   (local/self-hosted inference reports progress per-page naturally, which
   is arguably easier to surface than the current streaming-token-count
   proxy) — whichever adapter lands in Phase 2 needs its own
   `stage_current`/`stage_total` wiring note, not a silent reuse of
   `dashscope_ocr.py::_mm_stream`'s counting logic.
2. **Spec-text stays a validation signal, not an alignment key**
   (HANDOFF §5.7) — this holds regardless of which model produces the spec
   text. Do not revert this because a new model happens to read specs more
   reliably; the reasoning behind demoting spec-text was about alignment
   robustness, not about any one model's current accuracy.

## 10. Open items

- Hengtong 65.4% root cause (§6, row 6) — not resolved, in scope for Phase 2.
- Run-to-run stability — zero data yet on the candidate; Phase 2 requires it.
- Duplicate-copy (正本/副本) handling on the candidate — zero data yet;
  Phase 2 requires it (上海浦东 is the designated poison sample).
