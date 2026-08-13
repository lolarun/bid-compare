# 24 — Compare Workspace Redesign (three-stage flow + unified doubt inbox)

> **Status: CONFIRMED — all design decisions resolved with the user 2026-08-12,
> ready for implementation (not yet started).**
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

## 4. Backend changes (deliberately small)

The seven authoritative services, tier semantics, idempotent batch-confirm,
and submission identity are all **unchanged**. Three additions:

- **B1 Multi-sheet tender Excel.** `parse_tender_xlsx` gains sheet awareness:
  new preview response field `sheets: [{name, looks_like_list, row_count}]` +
  request param `sheet`; auto-detect = first sheet whose header matches the
  existing `_find_header_row` heuristics. No format-guessing beyond what the
  current parser already does.
- **B2 Per-stage progress.** `ExtractionJob` (+ API) gains
  `stage_current: int | null`, `stage_total: int | null` (e.g. pages done/total
  within 逐页识别). Emitted where the pipeline already knows these counts;
  `progress_pct` kept for compat.
- **B3 Dry-run confirm.** `batch-confirm` gains `dry_run: bool = false`: runs
  the exact same validation path (structural rows, missing totals, checksum,
  alias conflict) but rolls back before persisting, returning the structured
  issues as a normal 200. The inbox calls it once per recognized file, so
  every issue that today ambushes the user at confirm-time is visible *before*
  they click anything — with zero duplicated validation logic and zero drift
  risk between "what the inbox says" and "what confirm enforces".

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

- **Recognition-layer instability** (feedback #9: 远东 count differs across
  runs; 浦东 272 = 2×136 suspected double-recognition). Separate
  investigation track — a UI redesign must not paper over a recognition bug.
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

## 8. Implementation order

1. B1 + B2 + B3 backend (each independently testable; B3 gets contract tests
   asserting dry-run persists nothing and returns identical issue payloads to
   the real confirm path).
2. `useDoubtInbox` + copy table (pure frontend, testable against fixtures).
3. `MaterialsStage` (new cards + slots) behind the existing route.
4. `InboxStage` + `ResultsStage` assembly; retire the wizard; route redirects.
5. Manual re-test with prj2 (the exact dataset that exposed all of this),
   then update this doc's status banner + supersede note in design/06.
