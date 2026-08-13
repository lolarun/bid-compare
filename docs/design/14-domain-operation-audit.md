# Domain Operation Audit (P1-4) Design — not yet scheduled

> **Status — audited 2026-06-23.** Partially implemented. The design was originally deferred, but P1-4 has since shipped (per TODO.md, commit `bdaa890`): Plan B was chosen but realized with a single `OperationLog.payload` JSON column and a new `write_domain_event()` helper, NOT the six flat columns / extended `write_log()` this document prescribes. The open questions in §5 remain unscheduled.
> _Originally written 2026-06-22. English translation of the Chinese original; now the authoritative version._

> Design date: 2026-06-22
> Status: **design-first, not implemented** (user decided to defer on 2026-06-22).
> Basis: `docs/design/12` P1-4 / §4; CLAUDE.md §6/§10; `.claude/rules/database-safety.md`.

> (corrected 2026-06-23: the "not implemented" status above is stale. P1-4 was implemented on 2026-06-22 (TODO.md, commit `bdaa890`). See the per-section corrections below for how the shipped form differs from this design.)

## 1. Current state (verified at file:line)

| Fact | Location |
|------|----------|
| The only audit model = `OperationLog`, flat fields user/module/action/target/result/remark/created_at | `models/operation_log.py:9-19` |
| The only write helper = `write_log()` | `routes/logs.py:58-80` |
| **`write_log` is called in only 4 places, all auth/user-management**: login, add/disable/delete user | `auth.py:74`, `users.py:59,94,113` |
| **Bid-comparison domain operations have zero audit**: confirm/correct/exclude/rematch/finalize/historical-import write no log | none |
| Existing consumers: `GET /logs` pagination + export; the frontend has a log-viewing page | `routes/logs.py:12`, `apps/www` `/logs`, `/export/logs` |

Core conclusion: domain operations currently have **no audit trail at all**; and there is a **ready-made read-only `OperationLog` viewer**.

## 2. Plan comparison

### Plan A: a new standalone `DomainAuditEvent` table (the original audit plan)
- New table (event_type/project/session/submission/row/actor/before_json/after_json/created_at) + a `record_audit_event()` service + 6 instrumentation hooks + **a new GET read endpoint + a new frontend viewer page**.
- Faithful to the original audit text, with the cleanest structure.
- **Cost**: a complete subsystem. If you only build the table without wiring the read endpoint / frontend → a write-only dead table (against delivery ROI). Large effort, spanning backend + frontend.

### Plan B (recommended): extend `OperationLog` + reuse the existing viewer
- Add structured columns to `OperationLog`: `before_json` / `after_json` / `project_id` / `session_id` / `submission_id` / `row_id` (all nullable, via an idempotent Alembic migration).
- `write_log()` gains backward-compatible extra parameters, plus new domain-event constants (confirm/correct/exclude/rematch/finalize/history_import).
- Domain write points call the extended `write_log`.
- The existing `/logs` viewer and export are **reused directly** (add display of the structured fields where necessary).
- **Benefit**: one audit table + one viewer; small, low-risk, immediately visible.
- **Cost**: `OperationLog` carries two kinds (system operations + domain audit), distinguished by `module`/event_type; JSON columns and flat columns are mixed (acceptable).

> Recommend Plan B. It directly answers the audit's criticism that "the flat fields are insufficient" (adding before/after + identity) without spinning up a separate subsystem.

> (corrected 2026-06-23: Plan B was chosen, but the shipped form differs from this section's column layout. Instead of six flat columns (`before_json`/`after_json`/`project_id`/`session_id`/`submission_id`/`row_id`), migration `0003_audit_fields` adds a single nullable `operation_logs.payload` JSON column (`models/operation_log.py:20`). The structured shape — `{event_type, identity, before, after, meta}` — lives inside that JSON, where `identity` holds `project_id`/`submission_id`/`session_id`/`alignment_group_id`/`alignment_item_id`/`finalization_id`. The "before/after + identity" intent is preserved; only the storage shape changed from columns to nested JSON.)

## 3. Plan B implementation checklist (when implementing)

1. migration `0003_operation_log_audit_fields` (idempotent): add the 6 nullable columns + the necessary indexes (project_id/submission_id).
2. `models/operation_log.py`: add the corresponding columns.
3. `routes/logs.py::write_log`: extend with `before=None, after=None, project_id=None, session_id=None, submission_id=None, row_id=None`, serialize JSON; zero change to old callers.
4. `core/` or `services/audit.py`: domain-event-type constants + a thin `record_domain_event()` wrapper (unified `module` naming, actor resolution).
5. Instrumentation hooks (ordered by value/risk, can be batched):
   - High-value, low-risk first: batch-confirm (`routes/quotes.py`), anchor finalize (`routes/analysis.py:2349`), exclude/item-confirm.
   - Second batch: rematch (the match route), historical import (import_service).
6. `/logs` response/frontend: expose the structured fields (as needed).
7. Tests: write_log backward compatibility; domain events written with identity + before/after; `/logs` filtering.

> (corrected 2026-06-23: as-shipped vs. as-written, item by item.
> - **Item 1**: the migration is named `0003_audit_fields` (not `0003_operation_log_audit_fields`), and it adds the single `payload` JSON column (plus `bid_quote_lines.row_type` for P1-3) — not 6 columns. It is idempotent via an `inspect`-based `_has_column` guard. No dedicated `project_id`/`submission_id` indexes were added, since those now live inside the JSON payload.
> - **Item 2**: `models/operation_log.py` gained one `payload = Column(JSON, nullable=True)` field.
> - **Item 3**: `write_log()` in `routes/logs.py:58-80` was NOT extended — it still takes only `user/module/action/target/result/remark` and still auto-commits. The domain path is a separate helper instead (see item 4).
> - **Item 4**: implemented as `apps/api/services/audit.py::write_domain_event()` (not `record_domain_event()`). Crucially, `write_domain_event` does **NOT** commit — the caller commits in the same transaction as the business write (contrast `write_log`, which commits). It builds the `{event_type, identity, before, after, meta}` payload and a short `target` label.
> - **Item 5**: 7 hooks shipped, not the high-value-first / second-batch split written here. The canonical event types in `services/audit.py` are `bql_confirm` / `tender_session_confirm` / `alignment_group_confirm` / `alignment_item_confirm` / `alignment_bulk_confirm` / `alignment_finalize` / `llm_fill_persist`. Call sites: `routes/analysis.py`, `services/alignment/alignment_service.py`, `services/submission/quote_confirmation_service.py`. Note the shipped set does NOT include separate `exclude`, `rematch`, or `history_import` events named in this checklist.
> - **Item 7**: tests exist at `apps/api/tests/test_audit_events.py`.)

## 4. Relationship to other remediations
- Depends on P2-1 Alembic (ready): this design's schema change goes through a versioned migration.
- Same origin as P1-3 (row-level audit semantics): `updated_at` gives "when it changed," this design gives "who changed what, and the before/after values."
- With 10.4's status enum: event_type should be brought into the unified enum to avoid re-creating string literals.

> (corrected 2026-06-23: the schema change did go through versioned migration `0003_audit_fields` (Alembic, as promised). The event-type constants in `services/audit.py` (`EVENT_BQL_CONFIRM`, etc.) are centralized module constants, but they are NOT yet folded into a single shared 10.4 status enum — that consolidation remains open.)

## 5. Open questions (confirm before scheduling)
- Will domain audit and system operations share a table long-term (B), or is B a transition with a later split-out (A)?
- before/after granularity: whole-row snapshot vs. only-changed-field diff?
- Is a source identifier beyond actor needed (API / batch script / LLM suggestion)?
- Retention period / archival policy (the audit table will keep growing).

> (corrected 2026-06-23: these four open questions remain unresolved and unscheduled. The shipped implementation chose the Plan B shared-table route (question 1) by default, but the diff-granularity, source-identifier, and retention/archival questions are not addressed in code.)
