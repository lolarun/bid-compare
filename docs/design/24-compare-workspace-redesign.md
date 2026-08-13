# 24 — Compare Workspace Redesign (three-stage flow + unified doubt inbox)

> **Status: CONFIRMED — all design decisions resolved with the user 2026-08-12,
> ready for implementation (not yet started). Amended same day after an
> independent review (Fable): B0 added, B2/B3 re-sized from small to medium
> (§4), implementation order revised (§8) — all review claims re-verified
> against source before adoption.**
> Trigger: user's 2026-08-12 manual-test feedback (8 issues + 1 addendum), recorded
> verbatim in `docs/项目资料/用户反馈/2026-08-12/招标比价手工测试问题.md`. Issue #8
> ("the flow is fundamentally wrong — redesign it") is the driver; #1–#7 are mostly
> its symptoms. Direction decisions confirmed in §3, remaining questions resolved
> in §7. Supersedes the *frontend flow* portions of design/06 (bid-flow v2.3) once
> implemented; all backend semantics in design/05/06/12 remain authoritative.

## 1. Why the current flow fails (root causes, not symptoms)

The current UI is a 5-step wizard (config → tender list → quotes → alignment →
matrix) whose shape mirrors the **internal processing pipeline**, not the user's
task. Four structural problems:

- **P1 — Pipeline-shaped UX.** Step 2 renders the recognition pipeline itself as
  an 8-chip progress strip ("render PDF 10%", "merge results 88%"). Users see
  implementation stages that mean nothing to them (feedback #3), and the global
  0–100% axis is dominated by one long stage, so it visually stalls at
  "逐页识别 20%" for minutes (#4).
- **P2 — Human decisions scattered and dead-ended.** Six kinds of manual
  decisions live in 3 different steps with 4 different interaction styles.
  Three of them are dead ends: quality-tier banners show internal metric names
  with no action (#5), structural-integrity errors are toast-only with no
  "go fix it" path (#7), supplier-name conflicts use native `window.confirm` (#6).
- **P3 — Source model too narrow.** "PDF is the primary source, Excel is
  reference-only" fails on real tenders (prj2) where the PDF body has **no
  list** and the list lives in a separate, possibly multi-sheet Excel attachment
  (#1). Verified: the Excel-primary path exists in the backend
  (`source_type='excel'` in tender-list/confirm), but `parse_tender_xlsx` reads
  only `wb.active` — no sheet listing/selection — and the UI demotes Excel to a
  reference panel.
- **P4 — Gate semantics presented as terminal failure.** The quality gates
  themselves are charter invariants (§4: BLOCKED must never enter official
  data) and are **not** being relaxed. What's wrong is the presentation: a gate
  should produce a *workable task*, not a dead-end toast.

## 2. Target shape: three-stage workspace + unified doubt inbox

One workspace page per comparison task, three stages with status badges,
deep-linkable, no forced sequence — materials process in the background, doubts
accumulate into one inbox, results fill in progressively.

```
① Materials            ② Doubt inbox                  ③ Results
   project/category       ALL manual items as a          matrix + evaluation
   3 material slots       queue; each = one plain-       + AI explanation
   compact per-file       language sentence + a          + export/save/approve
   progress cards         "go handle it" jump            (pending excluded from
   (stage label +         directly into the place        totals — unchanged
   within-stage %)        where it can be fixed          three-tier gating)
```

**Stage ① Materials.** Three slots:
1. *Tender document* (PDF, optional) — contributes brand requirements,
   evaluation policy, cover scalars. May contain an embedded list or not.
2. *Procurement list* (the row axis; required before official results) —
   sources: Excel upload (**first-class primary**, multi-sheet:
   auto-detect the list sheet, show a sheet switcher for user override),
   or the embedded list recognized from the tender PDF. When both exist,
   the reconcile diff becomes an inbox item instead of a Step-1 blocking panel.
3. *Bid quotes* (batch drag-drop, unchanged upload mechanics via
   `useSupplierUpload`).

Every uploading/recognizing file renders one compact card: filename + current
stage in plain language + **within-stage progress** ("逐页识别 12/31 页"),
replacing the 8-chip strip. Detected supplier name is shown inline on the card
with a link-to-known-supplier dropdown; name conflicts get a warning tag on the
card (and an inbox item) — **never a blocking dialog** (user decision, §3).

**Stage ② Doubt inbox.** One queue aggregating every item that needs a human,
each rendered as a plain-language sentence + severity + a jump:

| Item type | Source | Jump target |
|---|---|---|
| Recognition quality REVIEW/BLOCKED | `job.result._quality` (exists today) | file's table-proof view (ExtractionEditor), banner rewritten in plain language |
| Structural rows (no total price, column misalignment, checksum mismatch) | **dry-run batch-confirm** (§4-B3) | the exact offending row, highlighted |
| Supplier name conflict / alias similarity | dry-run batch-confirm | inline resolver on the file card |
| Excel↔PDF list differences | existing reconcile | diff view with per-row add/ignore (exists) |
| Pending alignment cells | existing anchor-review matrix | review matrix cell (exists) |
| Missing-quote acknowledgment | design/23 missing-ack | review matrix cell (exists) |

Inbox empty ⇒ official results unlocked. Inbox non-empty ⇒ Stage ③ still shows
a *provisional* matrix (pending/unconfirmed excluded from totals — exactly the
existing three-tier gate semantics, unchanged).

**Stage ③ Results.** Current Step 4 essentially preserved (matrix, evaluation
cards, AI explanation, export, save version, approve). Post-B3 identity keys
already consistent.

## 3. Decisions already confirmed with the user (2026-08-12)

1. **Go straight to the full redesign (R-B)** — no interim patch round on the
   old wizard.
2. **Multi-sheet Excel: auto-detect + user-overridable sheet switcher.**
3. **Supplier-name conflict: never interrupt.** Recognized name is used as-is;
   conflicts become card annotations / inbox items. (Alias-merge decisions
   still gate that file's 入库 — master-data pollution rules unchanged — but
   the gate is a visible task, not a modal ambush.)

## 4. Backend changes

> Sizing revised 2026-08-12 after an independent review (Fable) whose three
> code-level claims were all re-verified against source: B3 and B2 are **not**
> small items, and a new B0 must go first. The seven authoritative services,
> tier semantics, idempotent batch-confirm, and submission identity remain
> **unchanged**.

- **B0 (NEW, goes first) — `copy_no` downstream handling.** The recognition
  prompt deliberately outputs duplicate list copies in full, tagged `copy_no`
  (`vl_quote.py:102`) — but `grep apps/api/services/ copy_no` returns zero
  consumers: confirm/checksum/row-count logic sums across all copies blindly.
  This fully explains the 浦东 case (272 = 2×136 rows, duplicate share exactly
  50%, declared_total_diff ≈ doubled) — a deterministic defect, **not**
  recognition instability. Fix at confirm time: select one copy per submission
  (reference implementations already exist in the test layer —
  `test_cable_accuracy_e2e.py::_best_copy` picks the copy whose sum is closest
  to the declared total; `test_cable_golden.py` picks the lowest copy_no
  deterministically), keep the others as draft evidence. Must land **before**
  B3: otherwise the dry-run inbox will report this flagship test dataset as a
  false "row count vs declared total" doubt on day one.
- **B1 Multi-sheet tender Excel** (small, as designed). `parse_tender_xlsx`
  gains sheet awareness: preview response field
  `sheets: [{name, looks_like_list, row_count}]` + request param `sheet`.
  Auto-detect default: among sheets whose header matches the existing
  `_find_header_row` heuristics, pick the one with the **most data rows** —
  not the first match, because real attachments often lead with a summary
  sheet whose header looks list-like.
- **B2 Per-stage progress** (medium — needs a new data source for the longest
  stage). API fields `stage_current: int | null`, `stage_total: int | null`
  as designed, `progress_pct` kept for compat. But the dominant stage
  (识别报价清单, `_notify(...55)` → black box until 80) is a single streamed
  model call with **no page-level progress available** — page counts only
  exist for the short render/orientation stages. The DashScope call is already
  streaming (`_mm_stream`, `incremental_output=True`), so the long stage
  reports **transcribed-line count** ("已转录 N 行", monotonically increasing,
  no total) by counting newlines in the stream callback; short stages use page
  counts. Without this, new file cards would sit frozen on "识别中" and
  reproduce feedback #4.
- **B3 Dry-run confirm** (medium — requires a behavior-preserving collector
  refactor, not just a rollback flag). Two verified facts the original sizing
  missed:
  1. The confirm path is **fail-fast**: 16 `raise` points in
     `quote_confirmation_service.py`, first gate hit throws. A naive
     "run + rollback" dry-run would surface only the *first* problem per
     call — the user clears one, refreshes, sees the next: the exact
     ambush-one-at-a-time experience this redesign exists to kill. The gates
     must be refactored to **collector mode** (accumulate issues, then decide
     raise-or-return at the end). Behavior-preserving for the real path;
     contract tests must assert *bidirectionally* that (a) dry-run's issue
     set is identical in shape and content to what the real path raises
     one-by-one, and (b) dry-run leaves the DB byte-identical.
  2. Two early-exit branches **commit mid-function** (`:379` idempotent-hit
     lifecycle write, `:432` empty-items) and the revival branch deletes stale
     rows (`:394`); a trailing rollback cannot un-commit `:379`. dry_run needs
     explicit guards at these points.
  With that done, the payoff stands: every issue that today ambushes at
  confirm-time becomes visible before any click, with zero duplicated
  validation logic and zero drift between inbox and confirm.
- **B4 Dry-run caching.** Inbox re-entry must not re-validate N files every
  time: cache dry-run results keyed by `job_id + hash(items)`; editing the
  table invalidates and re-runs that file only.

## 5. Frontend architecture

- Route: keep `/compare/:projectId/:stage?` (3 stages instead of 5 steps;
  old step indices redirect: 0,1,2→materials, 3→inbox, 4→results).
- Decompose `compare/IndexView.vue` (2.7k lines) into
  `MaterialsStage.vue` / `InboxStage.vue` / `ResultsStage.vue` +
  a `useDoubtInbox` composable that aggregates the table in §2 from existing
  data (`_quality`, dry-run results, reconcile result, review-matrix counts).
  `useSupplierUpload` (already extracted in R5) is reused as-is;
  `AnchorReviewMatrix.vue` / `BidMatrix.vue` / `ExtractionEditor.vue` are
  embedded, not rewritten.
- Plain-language copy table: one module mapping internal signals
  (`no_seq_rows`, `bbox_coverage`, `declared_total_diff`,
  `row_conservation_unverifiable`, `missing_total`, …) → user sentences with
  a "what you can do" clause. Internal metric names never rendered raw.

## 6. Explicitly out of scope

- **Recognition-layer instability — now narrowed to the 远东 case only.**
  Feedback #9 split after verification: 浦东 (272 = 2×136) is a deterministic
  downstream defect fixed by B0, **in scope**. 远东 (row count drifting across
  runs of the same PDF) is genuine recognition nondeterminism — separate
  investigation track, same family as the known orientation-detection
  instability; does not block any step here and can run in parallel.
- Evaluation/recommendation semantics, export content, historical-price
  governance: untouched.
- Invite (邀标) page flow: untouched this round (multi-sheet Excel parser from
  B1 benefits it later for free).

## 7. Resolved questions (user decisions, 2026-08-12)

1. **AUTO-tier confirm ergonomics → one-click bulk confirm.** Files whose
   dry-run is clean and tier=AUTO get a "confirm all clean files" bulk action
   (charter's draft→fact user-confirmation requirement is satisfied by the
   explicit click; it just covers N files at once). Files with any inbox item
   keep per-file handling.
2. **Provisional matrix visibility → always visible + prominent banner.**
   Stage ③ renders as soon as ≥1 submission is confirmed, with a
   "provisional — N doubts unresolved" banner. Pending/unconfirmed stay out of
   totals (existing gate semantics); export / save version / approve remain
   locked until the inbox is empty.
3. **Dual list sources → the list slot is primary, explicitly.** Whatever the
   user placed in the *procurement list* slot is the primary source. The
   tender-PDF embedded list only auto-fills the slot when it is empty; once
   filled, the embedded list is reference-only and differences surface as
   inbox items. Primary-ness is expressed by user action, never guessed.

## 8. Implementation order (revised with review input)

0. **B0 copy_no downstream handling** — removes the 浦东 false-doubt source
   before the inbox exists to report it; regression-testable against prj2.
1. **B1 multi-sheet** — unblocks the user's ongoing prj2 manual testing.
2. **B3 collector refactor + dry_run** (medium; bidirectional contract tests
   per §4-B3) + **B4 caching**.
3. **B2** including the streamed line-count data source.
4. `useDoubtInbox` + plain-language copy table (pure frontend, fixture-tested).
5. `MaterialsStage` behind the existing route; then `InboxStage` +
   `ResultsStage` assembly; retire the wizard; route redirects.
6. Manual re-test with prj2 (the exact dataset that exposed all of this),
   then update this doc's status banner + supersede note in design/06.

远东 instability investigation runs in parallel; blocks nothing.

### Acceptance-checklist additions (so small items don't silently vanish)

- Feedback #2 (reference-panel pagination) is absorbed by the redesign, not
  patched — acceptance must include an explicit "92-row list, page through
  all pages" case.
- **Inbox-clearing ack semantics**: every doubt type requires an explicit
  resolve/ack action to leave the queue (design/23 precedent — "看过了"
  doesn't count), otherwise the export/save/approve unlock gate is
  decorative. REVIEW-tier items clear only via confirm-after-proofread or an
  explicit "confirmed as-is" ack.
- During implementation, design/06's frontend-flow sections carry a
  "being superseded by design/24" banner from the first R-B commit onward —
  single-source-of-truth rule; the final supersede note lands at completion.
