# 26 — Recognition Engine Replacement (PaddleOCR-VL for all scanned PDFs)

> **Status — CONFIRMED, 2026-08-13. Ready for implementation (not started).**
> Originally scoped as "candidate evaluation, scanned bid PDFs only"; re-scoped
> and finalized after two user decisions (2026-08-13):
>
> 1. **PaddleOCR-VL becomes the sole visual recognition engine for ALL scanned
>    PDFs — tender and bid alike.** Routing is by actual PDF characteristics,
>    never by document type (the "bids are scans, tenders are native" pattern is
>    a property of the current 7-document sample, not of the population —
>    CLAUDE.md §1 forbids baking sample properties into architecture).
> 2. **Speed priority ("尽快") compresses the process, not the acceptance
>    bars.** The standing production shadow phase is replaced by a one-shot
>    offline dual-engine comparison batch (§7); the four acceptance gaps in §6
>    (field-level accuracy, duplicate copies, mixed orientation, run-to-run
>    stability) are the go/no-go itself and are NOT compressible.
> 3. **Direct replacement — no runtime qwen fallback, no engine config flag,
>    no dormancy period** ("直接替换", 2026-08-13). Paddle-BLOCKED documents
>    dead-stop honestly into the doubt inbox with the existing manual/Excel
>    recourse; qwen and its DashScope dependency are deleted in the same
>    round as the cutover. The project is pre-launch on a dedicated branch —
>    git is the rollback mechanism, dead code kept "just in case" is not.
>
> Track A (tender text-layer direct extraction, design/25) is **kept in front
> of** the new engine — it is deterministic parsing, not a recognition model,
> costs nothing, and was just shipped and accepted. Replacing it with Paddle
> would buy no simplification.
>
> Basis: CLAUDE.md §1/§4/§6, `.claude/rules/recognition.md`,
> `.claude/rules/tests.md`. Evidence: `scripts/{try_paddleocr_vl,
> score_paddleocr_vl,stitch_vs_multi_bench}.py` + `outputs/{baidu_paddleocr_vl,
> baidu_unlimited_ocr}/` (gitignored — rerun the scripts to reproduce) +
> `HANDOFF.md`'s 2026-08-13 merge-checkpoint section.

## 1. Target end-state: three-layer routing, all by PDF reality

```
usable text layer?  ──yes──▶  text-layer direct extraction (design/25 Track A,
        │                     deterministic, no model — tender today; quote-side
        no                    extension "Track A′" is a separate later decision)
        ▼
scanned document    ───────▶  PaddleOCR-VL  (the ONE visual engine, both doc types)
        │
        BLOCKED / untrusted (document-level, labeled — never silent)
        ▼
                              honest dead-stop: quality gate → doubt inbox →
                              manual recourse (Excel direct upload via
                              tabular_ingestion, or re-scan). NO fallback engine.
```

**No runtime engine fallback — user decision 2026-08-13 ("直接替换").** The
charter requires honest BLOCKED, not a backup model; the manual escape hatch
(deterministic Excel upload) already exists in production. This also applies
design/21's own lesson: the legacy chain kept "as fallback" became unreachable
dead code that later required its own physical-deletion round — a dormant
qwen fallback would repeat exactly that.

Every hop is document-level either/or with an honest `parser_mode` /
`input_mode` label (`text_layer` / `paddle_vl`) — the same boundary pattern
design/25 established and `.claude/rules/recognition.md` now codifies. What
stays forbidden: within-document per-table routing, and capability-sniffing
silent degradation.

## 2. Why this is an engineering project, not a config flip

`vl_quote.py`'s recognizer talks to the rest of the pipeline through one
contract: `ExtractionDraft`, built by `build_draft()`. Alignment, the quality
gates, `copy_no` dedup, dry-run confirmation, and every design/24 frontend
surface consume that shape and nothing upstream of it. Swapping the engine
behind that boundary is architecturally invisible downstream — **provided**
the new path fills the same shape with the same honesty guarantees. Three
things qwen does via prompt rules have no counterpart in PaddleOCR-VL (a
non-conversational document parser) and must be reimplemented
deterministically in the adapter: `row_type` classification, page attribution
(Paddle parses per page — natively better than qwen's self-reported pages),
and **duplicate-copy detection** (Paddle has no `copy_no` concept; §6).

## 3. Evidence gathered so far (2026-08-12/13 exploratory session)

All of the following is real, measured, and reproducible via the scripts named
in the header — not modeled or estimated.

### 3.1 Image-stitching is a dead end

Tested whether combining N page images into one composite image would save
time. Result: yes, materially (467s → 188s on 凯硕新正/19 pages), but only by
degrading resolution enough to lose ~1/3 of rows (100% → 67.4% recall).
Attempts to preserve resolution produced 37-96MB payloads that failed to
upload at all (reproduced 3×). **Closed: not a viable direction.**

### 3.2 PaddleOCR-VL — the candidate

0.9B-parameter open-source vision-language model purpose-built for document
parsing (tables/formulas/seals/skew), self-hostable (vLLM/SGLang/llama.cpp)
or via Baidu Cloud's hosted API (same OAuth credentials already in
`apps/api/.env` as the `BAIDU_UNLIMITED_OCR_*` keys). Published: 96.3%
OmniDocBench v1.6, table TEDS 91.71/94.67, cross-page table merging (v1.5+),
1.22 pages/sec at vLLM throughput.

**Measured on this project's own 7-document corpus** (row-level
sequence-presence recall — see §3.3 for what this does and does not prove):

| Document | Pages | Time | Seq recall | Has 序号 column |
|---|---|---|---|---|
| 凯硕新正 | 19 | 34-57s | 100% (89/89) | Yes |
| 泰科龙 (hardest known case, transposed table) | 53 | 70-85s | 100% (89/89) | Yes |
| 远东 | 19 | 43s | 100% | Yes |
| 上海绵存 | 31 | 59s | 100% (89/89) | No — content-align verified |
| 上海浦东 | 15 | 45s | 100% (136/136) | No — content-align verified |
| 宏胜 | 11 | 20s | 100% (136/136) | No — content-align verified |
| 亨通 | 11 | 24s | **65.4%** | No — content-align verified |

vs. current VL-direct baseline: ~630-710s (orientation pre-check + extraction)
for a comparably-sized document. **9-20× time advantage**, and **100% row
coverage on 泰科龙** — the document that defeated every prior fast-path
attempt (this session's stitching, and the 2026-07-13 Baidu OCR run's 59-70%).

### 3.3 What the metric does and does not prove

"Seq recall" checks row-identity presence (printed 序号, or content-aligned
name+spec+qty when absent). It does **not** check per-field correctness. An
exploratory field-level scorer hit a real, diagnosed limitation: empty cells
are not consistently represented as placeholders in Paddle's `matrix`/`cells`
output, causing column-position drift a fixed-header-index mapper cannot
safely resolve. **Field-level accuracy is not yet a trustworthy number** —
solved once, correctly, in the Phase P1 adapter, never再 in throwaway scripts.

## 4. Compressed implementation plan (~4-5 working days)

| Phase | Content | Est. |
|---|---|---|
| **P0** | Evidence infrastructure: parameterize the two exploratory scripts to `scripts/` conventions; bind `outputs/baidu_paddleocr_vl/` artifacts to code SHA / PDF SHA / model version in `docs/data/source_registry.json` | 0.5d |
| **P1** | **Adapter + provider** (critical path, §5) | 1-2d |
| **P2** | **Acceptance matrix, one shot** (§6) — **go/no-go lives here** | 1d |
| **P3** | Threshold spot-recalibration (§8) | 0.5d |
| **P4** | Production wiring (§9) | 0.5d |
| **P5** | prj2 full-flow UI regression: upload → doubt inbox (dry-run, copy-dedup plain-language item) → matrix → export, in the browser | 0.5d |

## 5. P1 — adapter and provider

Core trick: **the adapter serializes Paddle's `cells` matrix into canonical
CSV (`csv.writer`, empty cells as explicit empty fields) and feeds the
existing `build_draft()`** — `parse_csv`, the four gates, quality tiers and
the ledger run unchanged, so field-level validation comes for free and there
is zero drift between "what the eval says" and "what production enforces"
(the same zero-drift argument as design/24's dry-run). The adapter itself
implements the three prompt-rule replacements from §2:

- `row_type`: deterministic keyword rules (小计/合计/总计) — no model judgment;
- `page`: from Paddle's native per-page structure;
- **copy detection**: structural — sequence-axis restarts and/or repeated row
  blocks synthesize `copy_no` groups feeding the existing B0 dedup unchanged.
  This detector also hardens the qwen path (which today depends on the model
  honoring prompt rule 3), so it is shared infrastructure, not Paddle-only.

Deployment for evaluation **and first production cut: cloud API** (zero ops).
Self-hosted vLLM is a WSL2/GPU project on this Windows machine — a cost-
reduction second phase, incompatible with "尽快", and the "1.22 pages/sec"
number must not anchor the phase-1 decision.

## 6. P2 — acceptance matrix (thresholds user-accepted 2026-08-13)

Scoring = the production gates + `scripts/e2e_diff.py` vs golden, through the
P1 adapter. **This is a real gate: if the bar fails, the fast track stops
here** and the fallback discussion is a hybrid design, not a forced cutover.
The adapter survives either outcome (it is engine-agnostic evidence
infrastructure).

| Dimension | Sample | Pass bar |
|---|---|---|
| Row recall / precision | 7 cable + valve documents | ≥ qwen baseline |
| Numeric triple + spec text | Same, vs golden | ≥ 96% (incl. spec — qwen's weak point) |
| **Mixed orientation** | 宏胜 (180° mixed poison sample) | Passes without external orientation pre-check; contingency: keep `detect_rotations` in front of Paddle (cheap relative to Paddle's runtime) |
| **Duplicate copies** | 上海浦东 (272 = 136×2 raw rows) | Adapter's copy signal feeds B0; final row count returns to 136 |
| **Run-to-run stability** | Each document ≥3 runs | Amount-delta distribution, never a single-run number (HANDOFF §6 lesson 1 — qwen itself swings 0.18% between runs) |
| 亨通 65.4% | Dedicated root-cause via the proper adapter | Model vs scorer vs golden-side error resolved before any 亨通 accuracy claim |

## 7. P2-adjunct — one-shot dual-engine comparison batch (replaces shadow)

The previously-planned standing production shadow is **dropped**: the product
is in manual-testing stage, a long shadow adds latency without adding
decision-relevant data. Instead: run both engines once over the 7-document
corpus + prj2's 4 bids, machine-diff the two `ExtractionDraft` sets
(`block_alignment`-style), file the diff report with the P2 results. Same
evidence, days earlier.

## 8. P3 — threshold recalibration

`INTEGRITY_*`/`SEQ_*` were calibrated on qwen VL-direct's failure-mode
distribution (drop-rate/amount-defect correlation, truncation shape, seq
coverage). After the P2 runs, inspect each gate's hit distribution on Paddle
output; adjust **only** gates that demonstrably misfire, each change carrying
its derivation in the comment (existing convention). Silently inheriting
qwen's calibration risks systematic false pass/fail on the new path.

## 9. P4 — production wiring (direct replacement, no engine flag)

- **No `RECOGNITION_ENGINE` config** — a switch with one valid value is
  noise (user decision: 直接替换). The cutover is a code change; rollback is
  a git revert.
- `parser_mode="paddle_vl"` honest label (never impersonates `vl_direct`).
- Paddle BLOCKED/untrusted → the document stays BLOCKED per the existing
  quality-tier semantics, visible in the doubt inbox; recourse is the
  existing manual/Excel path. No second engine is invoked.
- `.claude/rules/recognition.md` first bullet amended again: "the sole visual
  engine is PaddleOCR-VL (`paddle_vl`)" — single-source-of-truth obligation,
  do NOT leave the rule saying VL-direct is sole while code says otherwise.
- design/24 B2 progress: the "已转录 N 行" counter is qwen-streaming-specific;
  the Paddle path reports `stage_current/stage_total` **per page** (natively
  available, simpler than the token-stream proxy).

## 10. qwen deletion (same round as cutover)

P4 deletes the qwen path outright — `vl_quote.py`'s VL-direct recognizer, the
3-vote orientation pre-check, `dashscope_ocr.py`'s quote-side surface
(`_mm_stream` and friends), and the DashScope config/keys. No dormant module,
no unreachable branch: design/21's legacy chain already demonstrated where
"keep it as a fallback" ends up, and this project is pre-launch on its own
branch, so `git revert` is the rollback mechanism.

`vl_tender.py` stays only if the tender path still needs it after the Paddle
adapter covers scanned tenders — resolve that during P4, don't leave it
ambiguous. The "targeted re-read" concept loses its designated engine; if it
is ever funded, engine selection restarts from evidence.
**Not separately funded**: orientation-precheck optimization — the 3-vote
orientation cost disappears wholesale with the qwen path.

## 11. Risks (stated up front)

1. **P2 is a genuine gate, not a ritual** — field-level accuracy is unknown
   today; the plan front-loads it to day 2-3. Worst case loses only the P1
   adapter, which is reusable for any future candidate.
2. **Cloud API quota/rate limits** — the 2026-07-13 Baidu experiment hit
   quota exhaustion mid-run; the ≥3-run stability requirement doubles as a
   rate-limit probe. Write observed limits into the P2 report.
3. **Bid documents to a third-party cloud** — same class as the existing
   DashScope flow, but a different vendor; worth one confirmation against
   the customer contract before production default flips.
4. **No automated recourse for Paddle-BLOCKED scans** (consequence of the
   no-fallback decision): a layout Paddle chokes on has only the manual/
   Excel path until re-scan. Accepted trade-off — P2's corpus bounds the
   expected BLOCKED rate before cutover, and the project is pre-launch.

## 12. Open items

- 亨通 65.4% root cause — in scope for P2.
- Run-to-run stability — zero data on the candidate; P2 requires it.
- Duplicate-copy handling — zero data; P2 requires it (浦东 is the poison
  sample).
- Track A′ (quote-side text-layer probe): out of scope here; decide after
  measuring the native-PDF ratio among historical bid uploads.

## Appendix — superseded content

The original "proposed" version of this document scoped the engine swap to
scanned **bid** PDFs only and planned a standing production shadow phase
(old §7). Both were superseded by the 2026-08-13 user decisions recorded in
the status banner; the evidence sections (§3) carry over unchanged.
