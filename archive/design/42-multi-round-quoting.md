# 42 — Multi-round quoting and price-trend comparison

> **Status: P0 + P1 implemented and tested 2026-08-26.** `QuoteRound`
> (migration `0009_quote_rounds`), submission→round attachment, the
> quote-rounds CRUD API, and `anchor_uid` generation/carry-over (migration
> `0010_anchor_uid`) are live — see §4.1's correction (discovered mid-build)
> and §7.4 for exactly what shipped vs. what's still P2/P3 plan only. P2
> (round-scoped alignment, round_trend) and P3 (creator/role) remain
> unimplemented plan.

## 1. Trigger

User, 2026-08-26:

1. A project is created by the project-management department.
2. Tender staff see the project, then upload the procurement list and the quotes.
3. Quotes arrive in several rounds — round 1 / 2 / 3. A collection round may
   happen *before* the tender itself, and staff must be able to name each round
   themselves.
4. There is exactly **one** procurement list, and it must be editable.
5. Comparison must show each supplier's price trend across rounds — the
   discount trend.

Points 1, 2 and 4 are close to what exists. Point 3 introduces an entity the
data model does not have. Point 5 is only meaningful if point 3 and point 4
are reconciled, which is where the real design work is.

## 2. What exists today (read from code, 2026-08-26)

| Fact | Location |
|---|---|
| `Project` has no creator or department column; `POST /api/projects` has no `require_role` — any authenticated user can create one | `apps/api/routes/projects.py` |
| `TenderListSession(project_id, category)` carries **two** responsibilities: the procurement-list version (`version` / `is_current` / `anchors_json`) **and** the comparison scope (`confirmed_supplier_ids` / `used_submission_ids`) | `apps/api/models/tender_list_session.py` |
| A hard gate validates `used_submission_ids` set integrity; matrix and export both read it | `apps/api/routes/analysis.py:232`, `apps/api/services/matrix/bid_export_service.py:46` |
| `BidSubmission` has `project_id` / `supplier_id` / `batch_id` — **no round** | `apps/api/models/bid_submission.py` |
| Alignment-group identity is `(project_id, category, tender_list_session_id, anchor_seq)`; `BidAlignmentItem` already carries `submission_id` | `apps/api/models/bid_alignment.py` |
| Roles are 管理员 / 比价员 / 查看者 only | `docs/design/16-user-role-management.md` |

## 3. The three problems rounds create

### 3.1 Comparison scope is stored on the wrong object

Round scope — which suppliers and which submissions belong to this round —
must live on the round. Leaving it on `TenderListSession` means round 2
overwrites round 1's `used_submission_ids`, and round 1's matrix can never be
reproduced. Reproducibility of a closed round is not optional: it is what the
award decision was made on.

### 3.2 Anchor identity does not survive a list edit — this breaks the trend

Requirement 4 (one editable list) and requirement 5 (cross-round trend)
collide. Editing the list today creates a new `TenderListSession` version, and
`tender_list_session_id` is part of alignment-group identity. After one edit,
round 1's rows and round 2's rows are no longer the same row to the system, and
the trend silently breaks apart.

The fix is a **stable `anchor_uid`** that survives list revisions. This is the
one part of this design that is expensive to retrofit later, because by then
there is trend data keyed on the wrong identity.

### 3.3 A pre-tender round has no anchor

Requirement 3 allows collecting quotes before the tender. At that moment no
confirmed procurement list exists, so per the charter's identity invariant only
a quote-derived axis is available, and that axis may feed the preview lane
only. A pre-tender round therefore cannot join the official trend until its
rows are aligned to the anchor set.

## 4. Data model

One new table plus four changes.

```text
QuoteRound  (new)
  id
  project_id, category                  -- same key as TenderListSession (§8 D2)
  seq                                   -- 1, 2, 3 … within (project, category)
  name                                  -- user-authored label (req. 3)
  stage         pre_tender | formal
  status        open | closed
  is_final_basis                        -- explicit, never auto (§8 D3)
  tender_list_session_id                -- list version this round was aligned under (provenance)
  confirmed_supplier_ids                -- moved off TenderListSession
  used_submission_ids                   -- moved off TenderListSession
  created_by, opened_at, closed_at, remark

BidSubmission        + round_id (nullable, indexed)
TenderListSession    each anchors_json row + anchor_uid
                     confirmed_supplier_ids / used_submission_ids → read-only legacy, no new writes
BidAlignmentGroup    + round_id, anchor_uid; identity becomes (project_id, category, round_id, anchor_uid)
                     tender_list_session_id kept as provenance (which list version this round matched against)
Project              + created_by_user_id
```

### 4.1 Correction (2026-08-26, during P0/P1 implementation): one group per (round, anchor), not one group for all rounds

The original plan below was wrong and is kept struck through rather than silently
edited, per the charter's retraction rule:

> ~~One alignment group per anchor, items partitioned by round. `BidAlignmentItem`
> already carries `submission_id`... single-round matrix = filter items by that
> round's submissions; cross-round trend = bucket the same group's items by round.~~

Reading `anchor_match.import_and_match` (the only place `BidAlignmentGroup` rows
are created) shows why: matching is **idempotent by wipe-and-rebuild**, scoped to
`(project_id, category)` only —

```python
old = db.scalars(select(BidAlignmentGroup).where(
    BidAlignmentGroup.project_id == project_id,
    BidAlignmentGroup.category == category,
)).all()
for g in old:
    db.delete(g)
```

With no round in that scope, running match for round 2 today would delete round
1's alignment rows outright — round 1's matrix could never be reproduced, and
there would be nothing left to compute a trend from. This is a real bug latent
in the moment rounds exist at all, independent of the trend feature.

**Corrected model:** one `BidAlignmentGroup` per `(round_id, anchor_uid)`. The
wipe-and-rebuild in `import_and_match` must scope its delete query by `round_id`
too, so re-matching round 2 only ever touches round 2's groups. `anchor_uid` is
then the join key **across** rounds' groups for trend purposes (§6), not the
row identity within a round.

This re-scoping touches `anchor_match.py`'s group-creation block and the
`/tender-list/match` route in `routes/analysis.py` (passes `round_id` through,
and `record_submission_scope` writes onto `QuoteRound` instead of
`TenderListSession`). Both are large, delicate, heavily-tested files. Given the
blast radius, **this re-scoping is moved into P2** (round_trend), done together
since P2 cannot work without it anyway — see the revised phase table in §7.4.
P1 ships `anchor_uid` generation and carry-over only (§4.2), which is
self-contained to `tender_session_service.py` / `tender_list.py` and does not
touch matching.

**Practical consequence:** until P2 lands, do not run `/tender-list/match` for
a second round against a project that already has a round-1 alignment result
you need to keep — it will still be wiped. P0/P1 store rounds and attach
submissions to them, but do not yet protect a prior round's alignment result
during a later round's match.

Both views consume one alignment result, which is what the "one business
result" invariant requires. The alternative — a group set per round — would
duplicate alignment work and let two rounds disagree about what a row *is*.

### 4.2 anchor_uid lifecycle

Assigned when a list version is first confirmed. On a revision, rows are
carried over by `seq` + name/spec identity; a row that survives keeps its
`anchor_uid`, a new row gets a new one, a removed row is marked `retired` and
stays visible in the rounds that used it.

## 5. Semantic rules

- **R1** A round is one complete, comparable quote set. Matrix, evaluation,
  export and recommendation must each state which round they computed, and must
  never mix rounds into one number.
- **R2** Official conclusions come only from the round flagged `is_final_basis`.
  Other rounds are explanatory — the same boundary the charter already draws
  around the LLM.
- **R3** A trend figure requires one basis: same `anchor_uid`, same
  `price_basis` (tax-inclusive vs exclusive), same quantity. When the basis
  differs the pair is marked not-comparable and **no discount number is
  emitted**. A discount computed across bases is a fabricated fact.
- **R4** A supplier absent from a round is reported as not-participating. Never
  zero-filled, never interpolated.
- **R5** The list stays editable. Rows keeping their `anchor_uid` keep their
  trend; new rows start their trend at the current round; retired rows keep
  their history.
- **R6** A `pre_tender` round stays in the preview lane until its rows are
  retro-aligned to the current anchor set (§8 D4).

## 6. Trend computation

New service `apps/api/services/matrix/round_trend.py`. It **consumes**
`BidMatrixService` output per round and derives deltas; it does not recompute
matrix semantics.

Outputs:

- **Row level** — per `anchor_uid` × supplier × round: unit price, total,
  round-over-round discount %, cumulative discount % vs the first participating
  round, participation state.
- **Supplier level** — per round: total, round-over-round discount %, total
  discount %, rank movement.
- Every figure carries `comparable: bool` plus a reason when false (R3).

## 7. API, frontend, migration, phasing

### 7.1 API

- `GET/POST /api/projects/{id}/quote-rounds`, `PATCH` for rename / close /
  set-final-basis. (P0)
- Intake / batch-confirm take `round_id`, defaulting to the current `open`
  round. (P0)
- `tender-list-match` takes `round_id`; `BidAlignmentGroup` wipe/rebuild scopes
  by round; `record_submission_scope` writes onto `QuoteRound`. (P2, per the
  §4.1 correction — bundled with `round_trend` since P2 cannot work without it)
- `GET /api/analysis/round-trend?project_id&category`. (P2)
- The `used_submission_ids` hard gates re-point at `QuoteRound` — two sites in
  `routes/analysis.py`, one in `bid_export_service.py`. (P2, same reason)

### 7.2 Frontend

> **First three bullets superseded by
> `docs/design/44-compare-entry-and-round-interaction.md` (2026-08-27)** —
> they were placeholders; design/44 is the actual interaction design
> (project-list entry, round bar, explicit new-round vs update-current-round
> semantics, read-only closed rounds, F1/F2/F3 phasing keyed to this
> document's P2/P3). The fourth bullet (procurement-list edit entry) is not
> yet designed anywhere — it remains a placeholder here.

- Project list page owns creation and entry; the workspace no longer invents
  projects.
- Workspace header gains a round selector (new / rename / close / set as
  basis). Uploads attach to the current round.
- A trend view: supplier discount lines plus per-row round columns.
- A procurement-list edit entry — one list, `version + 1` on save.

### 7.3 Migration (Alembic, idempotent — `_ensure_sqlite_schema` is frozen)

1. Create `quote_rounds`; for each existing `(project, category)` holding
   submissions, synthesize round 1 (`stage=formal`, `status=closed`,
   `is_final_basis=true`) and backfill `BidSubmission.round_id`.
2. Backfill `anchor_uid` into `anchors_json` from `seq`.
3. (P2) Add `bid_alignment_groups.round_id` + `anchor_uid`, backfill existing
   rows onto the synthesized round 1.

### 7.4 Phasing

| Phase | Scope | Delivers | Status |
|---|---|---|---|
| P0 | `QuoteRound`, `round_id`, uploads attach to a round | Rounds are stored; matrix behaviour unchanged | **Done** — backend only, see below |
| P1 | `anchor_uid` generation + carry-over on list confirm (additive only, does not touch matching) | List revisions no longer lose anchor identity — trend has something stable to join on once P2 exists | **Done** |
| P2 | `BidAlignmentGroup.round_id` + re-scoped wipe/rebuild (the §4.1 correction), `round_trend` service, API, trend view, export sheet | Requirement 5, and fixes the latent round-2-deletes-round-1 bug | **Pulled forward** (2026-08-27, design/44 D-2): next implementation step, before any round UI ships — so the UI never needs a "new round disabled/data-loss warning" transitional state |
| P3 | `created_by_user_id`, `POST /api/projects` restricted to 管理员 | Requirements 1–2 | Not started |

**What P0/P1 actually shipped (2026-08-26):** `QuoteRound` model + service
(`services/tender/quote_round_service.py`) + CRUD API
(`routes/quote_rounds.py`, `GET/POST/PATCH /api/projects/{id}/quote-rounds`);
`BidSubmission.round_id`, wired through `confirm_batch` (auto-opens round 1
when omitted, honors an explicit `round_id`); `anchor_uid` generation and
content-match carry-over in `tender_session_service.save_session`; migrations
`0009_quote_rounds` and `0010_anchor_uid` (both idempotent, both verified
against a hand-built legacy-shaped DB, not just a fresh one). 34 new tests,
all passing; full existing suite (1142 tests) re-verified green after each
migration fix. **Not built in this pass:** the round selector UI, the
procurement-list edit entry, and anything else under §7.2 (frontend) — this
was a backend-only pass. `tender-list-match` / `record_submission_scope` were
**not** touched, so a round's alignment is not yet protected from a later
round's match run (§4.1) — no round-scoped comparison or trend exists yet;
only storage and identity plumbing does.

P1 must land before P2. Building P2's trend math first, without P1's stable
`anchor_uid`, yields a trend that is wrong the first time anyone edits the
list — and wrong invisibly.

## 8. Decisions taken 2026-08-26

- **D1 — Roles: reuse the existing three.** No 项目管理部 / 招标人员 role is
  added. 管理员 stands in for the project-management department (creates
  projects), 比价员 for tender staff (uploads list and quotes). `Project` gains
  `created_by_user_id`; `POST /api/projects` gains `require_role(管理员)`.
  `docs/design/16` gains two matrix rows.
- **D2 — Round scope is `(project, category)`,** identical to
  `TenderListSession`, so valves and cable run their rounds independently and
  existing scoping code keeps its shape.
- **D3 — The basis round is set explicitly by a human.** No auto-promotion to
  the newest round. A round with no explicit basis flag produces no official
  evaluation, export, or recommendation — only preview.
- **D4 — Pre-tender rounds get retro-alignment.** Stored under a quote-derived
  axis as preview; once the list is confirmed, a retro-align action maps the
  round's rows onto the anchor set, after which it participates in the trend
  normally. Until then, R6 holds.

## 9. Open items (not decided, not in scope of this plan)

- How `anchor_uid` carry-over resolves an ambiguous edit (a row whose name and
  spec both changed — same row revised, or removal plus addition?). Needs a
  rule before P1 ships; the safe default is to treat it as removal + addition,
  because a wrong carry-over fabricates a discount.
- Whether a closed round can be reopened, and what that does to a matrix
  already exported from it.
- Whether the trend view belongs in export (P2 lists an export sheet; it is not
  yet agreed that the exported workbook should carry non-basis rounds).
