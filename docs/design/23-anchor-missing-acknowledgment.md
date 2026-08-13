# 23 — Persisting "Confirmed Missing" for Anchor-Review Cells

> **Status — implemented 2026-08-11.** Written in response to a task spun off
> during the frontend-review R1 remediation round (see
> `docs/design/22-best-practice-review-scope.md`). §10's three open questions
> were put to the user and all three resolved with the recommended option:
> single `POST .../missing-ack` with `acked: bool` (not split POST/DELETE);
> un-ack shipped in this same round; `reason` stays schema-only — backend
> accepts it, frontend does not send it.
>
> Implemented exactly as designed below, with one naming deviation: `reason`
> and index naming otherwise match §3/§7 verbatim. Delivered: model
> (`apps/api/models/anchor_missing_ack.py`), Alembic revision
> `0007_anchor_missing_ack` (verified against the live dev DB — table +
> indexes + unique constraint all landed correctly, confirmed via
> `alembic current` and a direct schema inspection), service
> (`apps/api/services/alignment/anchor_missing_ack.py`), route
> (`POST /analysis/anchor-review/missing-ack`), read-side integration in
> `build_anchor_matrix`'s review-matrix branch, and the frontend (`confirmMissing`
> replaced by an async `setMissingAck` that calls the real endpoint, plus a
> "取消确认" un-ack affordance — the fake-button UI copy from R1 is gone).
>
> Test plan from §9 executed in full: `apps/api/tests/test_anchor_missing_ack.py`
> (13 tests — service idempotency, session-scoping, matrix integration
> confirming `missing_acked` only flips for the exact acked pair, and the §6
> safety claim asserted directly (`cell_status`/`missing_cells`/
> `matrix_distribution` byte-identical before/after ack, not just claimed), plus
> route + audit-event tests). Full suite: `pytest apps/api/tests tests -q` →
> 767 passed (754 baseline + 13 new), same 4 pre-existing unrelated failures.
> `vue-tsc -b` and `vitest` (42/42) both clean.

## 1. The bug this closes

`apps/www/src/views/compare/components/AnchorReviewMatrix.vue`'s `confirmMissing()`
only writes to a local `confirmedMissing` ref. It never calls the backend. The
acknowledgment — "I've looked at this cell, this supplier genuinely has no quote
for this anchor, stop nagging me about it" — is lost on refresh. A frontend
review flagged this as a "fake button": the UI implies a real confirmation but
performs none. The R1 remediation round made the UI honest (relabeled to "本次
复核内标记", tooltip explains it's session-only) instead of implementing the
real fix, because the real fix turned out to need a schema decision.

## 2. Why this isn't a one-line backend call

A "missing" cell is not a row that exists and needs a status flip — it's the
**absence** of any `BidAlignmentItem` for that `(anchor_seq, submission_id)`
pair. Nothing was ever matched or proposed. Contrast with `cell_status='excluded'`,
which already works today via `POST /anchor-review/item-confirm`: that endpoint
flips `action` on an *existing* `BidAlignmentItem.id`. A missing cell has no
`item_id` to flip.

`BidAlignmentItem` also has a CHECK constraint (`apps/api/models/bid_alignment.py:49-54`,
enforced via a SQLite table-rebuild in `apps/api/core/database.py`'s
`_ensure_sqlite_schema`, v3.0 step):

```sql
CHECK (
    (quote_id IS NOT NULL AND bid_quote_line_id IS NULL) OR
    (quote_id IS NULL AND bid_quote_line_id IS NOT NULL)
)
```

Exactly one of the two FK columns must be non-null — that's the whole point of
the constraint (§7: "两种路径，必须且只能有一个非空"). A row representing "no
quote exists, and that's acknowledged" would need *both* null, which this
constraint forbids outright. Also: `_ensure_sqlite_schema` is frozen (per the
Alembic migration introduction, `docs/design/13`) — any new persisted state
must go through a real Alembic revision, not another hand-rolled step in that
function.

## 3. Two designs considered

### Option A — relax the CHECK constraint, represent the ack as a `BidAlignmentItem`

Add a new `action` value (e.g. `no_quote_confirmed`) and allow both FKs null
for that action. Reuses the existing group/item machinery; `build_anchor_review_matrix`
already joins through `BidAlignmentItem` for cell status, so a cell showing
`action='no_quote_confirmed'` would flow through the same path other actions do.

**Rejected**, for three reasons:
- It weakens an invariant (`exactly one of quote_id/bid_quote_line_id`) that
  other code may rely on holding unconditionally — every place that does
  `item.bid_quote_line_id or item.quote_id` and assumes one is always present
  would need re-auditing after this change, and I don't have full confidence
  I'd find every call site.
- A missing cell frequently has **no `BidAlignmentGroup` at all** for that
  anchor_seq (nothing matched for *any* supplier). Recording the ack would
  require find-or-create logic for a group that may otherwise never exist —
  extra branching in code that's already carrying a lot of matrix-building
  complexity.
- The migration itself is a full-table rebuild of `bid_alignment_items` (SQLite
  can't `ALTER ... DROP CONSTRAINT`), the same heavy operation the v3.0 step in
  `_ensure_sqlite_schema` already had to do once. Doing it again for a
  UI-acknowledgment feature is disproportionate risk for the payoff.

### Option B — a small, separate acknowledgment table (recommended)

A "confirmed missing" fact isn't a quote-item at all — it's reviewer metadata
about an *absence*. Model it as its own thing instead of stretching
`BidAlignmentItem` to cover a case its constraint was designed to exclude.

```python
# apps/api/models/anchor_missing_ack.py
class AnchorMissingAck(Base):
    """Reviewer's explicit acknowledgment that an anchor×submission cell has no
    quote and that's expected — not a data gap needing further matching.

    Pure audit/UI-suppression fact. Does NOT create a quote, does NOT change
    cell_status, does NOT affect evaluation totals or matrix_distribution — a
    missing cell stays 'missing' and excluded from pricing regardless of ack
    (see §6). Scoped to tender_list_session_id so a new tender-list version
    (re-numbered anchors) doesn't silently inherit a stale ack.
    """
    __tablename__ = "anchor_missing_acks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    category = Column(String(50), nullable=False)
    tender_list_session_id = Column(
        Integer, ForeignKey("tender_list_sessions.id"), nullable=False, index=True,
    )
    anchor_seq = Column(String(20), nullable=False)
    submission_id = Column(Integer, ForeignKey("bid_submissions.id"), nullable=False, index=True)
    reason = Column(String(500), default="")
    acked_by = Column(String(100), default="")
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint(
            "tender_list_session_id", "anchor_seq", "submission_id",
            name="uq_anchor_missing_ack",
        ),
    )
```

Why this is the safer choice:
- Zero changes to `BidAlignmentItem`/`BidAlignmentGroup` — the exclusivity
  constraint, and everything that depends on it, is untouched.
- No find-or-create-group branching — the ack doesn't need a group to attach to.
- The migration is a plain `CREATE TABLE`, not a rebuild of a large,
  heavily-read existing table. Lowest-risk shape available in this schema.
- Read integration is one extra query + one dict lookup per cell (§5), not a
  change to the group/item join logic that already computes cell status.

**Scope limitation, stated explicitly**: this only covers the modern
submission-mode path (`use_submission_path=True` in `build_anchor_review_matrix`,
i.e. `submission_id` as the authoritative column identity per CLAUDE.md §4/
`.claude/rules/bid-compare-backend.md`). The legacy `supplier_ids`-only review
path does not get this feature — consistent with the rest of the codebase's
direction of not adding new capability to the legacy identity path.

## 4. API surface

One endpoint, toggle-style (mirrors `anchor_review_item_confirm`'s
`action`-carries-the-intent shape rather than separate POST/DELETE routes):

```
POST /analysis/anchor-review/missing-ack
Body: { project_id, category, anchor_seq, submission_id, acked: bool, reason?: str }
→ { ok: true, anchor_seq, submission_id, acked }
```

- `acked=true`: idempotent upsert (re-confirming an already-acked cell just
  refreshes `acked_by`/`created_at`/`reason`, no error).
- `acked=false`: idempotent delete (un-acking an already-unacked cell is a
  no-op success, not a 404 — matches this codebase's existing idempotency
  convention, e.g. `supersede_submission`'s `already` flag).
- Resolves `tender_list_session_id` server-side from
  `get_current_confirmed_session(db, project_id, category)` — the request body
  doesn't pass it, same pattern as `anchor_review_finalize`.
- Audited via `write_domain_event` with a new `EVENT_ANCHOR_MISSING_ACK`
  constant in `apps/api/services/audit.py`, identity
  `{anchor_seq, submission_id}`, `after={"acked": body.acked}` — same shape as
  the existing `EVENT_ALIGNMENT_ITEM_CONFIRM` audit call it sits next to.

New Pydantic schemas in `apps/api/schemas/analysis.py`:

```python
class AnchorMissingAckRequest(BaseModel):
    project_id: int
    category: str
    anchor_seq: str
    submission_id: int
    acked: bool
    reason: str = ""

class AnchorMissingAckResult(BaseModel):
    ok: bool
    anchor_seq: str
    submission_id: int
    acked: bool
```

Service function, new small module `apps/api/services/alignment/anchor_missing_ack.py`
(alignment/ domain, next to `anchor_match.py`/`bid_alignment.py` per the
batch-6 package layout):

```python
def set_missing_ack(
    db: Session, project_id: int, category: str,
    anchor_seq: str, submission_id: int, acked: bool,
    reason: str = "", acked_by: str = "",
) -> AnchorMissingAck | None:
    """Idempotent upsert/delete. Returns the row on ack, None on un-ack."""

def get_missing_ack_set(db: Session, tender_list_session_id: int) -> set[tuple[str, int]]:
    """(anchor_seq, submission_id) pairs acked for this session — one query,
    called once per build_anchor_review_matrix invocation."""
```

## 5. Read-side integration

In `build_anchor_matrix`'s review-matrix branch (`apps/api/services/matrix/bid_matrix.py`,
the two `cell_status == CELL_MISSING` sites around lines 894-907 and 916-918),
after determining a cell is missing:

```python
acked_set = get_missing_ack_set(db, session.id)  # fetched once, before the anchor loop
...
if cell["cell_status"] == CELL_MISSING:
    cell["missing_acked"] = (seq_key, col_id) in acked_set
```

`ReviewCell` (Pydantic + `client.ts` TS interface) gains one field:
`missing_acked: bool = False`. `missing_reason` is untouched — the ack is an
annotation layered on top, not a replacement for the system-generated
explanation.

## 6. Why this cannot affect evaluation, totals, or export

This is the load-bearing safety argument, stated explicitly because
`.claude/rules/bid-compare-backend.md` is unambiguous that pending/excluded/
no-price rows must never enter evaluation totals:

- `cell_status` stays `'missing'` — unchanged. `missing_cells` counter,
  `quoted_count`, `covered_count`, `row_status`, `matrix_distribution` — none
  of them read `missing_acked`; they all key off `cell_status`, which this
  change never touches.
- `missing_acked` does not appear anywhere in `_evaluate_cell`
  (`apps/api/services/matrix/bid_evaluation.py`) or `_compute_recommendation`
  (`apps/api/services/matrix/bid_recommendation.py`) — both are unmodified by
  this proposal.
- The field is additive-only on `ReviewCell`, consumed by exactly one UI
  affordance (suppress the "未报价" nudge / show "已确认无报价"
  instead of a call-to-action). It has no other reader.

## 7. Migration

New Alembic revision, following the `0004_soft_fk.py` / `0005_tender_recommendation_snapshot.py`
convention (idempotent against both fresh DBs where `create_all()` already
made the table, and existing DBs that need it created):

```python
def upgrade() -> None:
    conn = op.get_bind()
    existing = sa.inspect(conn).get_table_names()
    if "anchor_missing_acks" not in existing:
        op.create_table(
            "anchor_missing_acks",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("category", sa.String(50), nullable=False),
            sa.Column("tender_list_session_id", sa.Integer,
                      sa.ForeignKey("tender_list_sessions.id"), nullable=False),
            sa.Column("anchor_seq", sa.String(20), nullable=False),
            sa.Column("submission_id", sa.Integer,
                      sa.ForeignKey("bid_submissions.id"), nullable=False),
            sa.Column("reason", sa.String(500), server_default=""),
            sa.Column("acked_by", sa.String(100), server_default=""),
            sa.Column("created_at", sa.DateTime),
            sa.UniqueConstraint(
                "tender_list_session_id", "anchor_seq", "submission_id",
                name="uq_anchor_missing_ack",
            ),
        )
        op.create_index("ix_ama_session", "anchor_missing_acks", ["tender_list_session_id"])
        op.create_index("ix_ama_submission", "anchor_missing_acks", ["submission_id"])

def downgrade() -> None:
    op.drop_table("anchor_missing_acks")
```

A brand-new table, additive-only — no existing-row migration, no FK/row-count
reconciliation needed (nothing to reconcile; the table starts empty).

## 8. Frontend changes

- `AnchorReviewMatrix.vue`: `confirmMissing()` becomes `async`, calls the new
  endpoint (pessimistic — wait for the response before flipping local state;
  this is a low-frequency, low-latency action, no need for optimistic UI).
  Button reverts to "确认缺报" (real confirmation again), tooltip removed.
  `confirmedMissing` local ref is replaced by reading `cell.missing_acked`
  directly from the matrix result — no separate client-side state to keep in
  sync.
- Add an "取消确认" (un-ack) affordance next to the acked state, calling the
  same endpoint with `acked: false` — the module docstring precedent in
  `bid_alignment.py` ("reversible and traceable") argues for this being
  in-scope alongside the ack itself, not a separate follow-up.
- `client.ts`: `ReviewCell.missing_acked?: boolean`; new
  `analysisApi.anchorReviewMissingAck(...)` wrapper in `api/index.ts`.

## 9. Test plan

- Unit: `set_missing_ack` idempotency (double-ack, double-unack, ack→unack→ack),
  uniqueness constraint holds, `get_missing_ack_set` scoping (doesn't leak
  across sessions/categories/projects).
- Integration: `build_anchor_review_matrix` returns `missing_acked=true` only
  for the exact acked pairs; unrelated missing cells stay `false`; `cell_status`/
  `missing_cells`/`matrix_distribution` are byte-identical before and after
  acking (asserts §6's safety argument, not just trusts it).
- Route: `POST /anchor-review/missing-ack` — ack, re-ack (idempotent), unack,
  re-unack (idempotent), audit event written.
- Full suite: `pytest apps/api/tests tests -q`, same baseline (754 passed, 4
  pre-existing unrelated failures) before declaring done.

## 10. Open questions for confirmation before implementing

1. Endpoint path/shape (`POST .../missing-ack` with `acked: bool`) — OK, or
   prefer separate POST/DELETE routes matching a different existing pattern?
2. Is the "取消确认" (un-ack) affordance actually wanted in this pass, or
   should it be deferred (ack-only, no undo UI yet)?
3. `reason` field — worth exposing as a UI text input now, or land as a
   schema-only field (backend accepts it, frontend never sends it yet) for a
   later round?
