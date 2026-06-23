# Introducing Alembic Versioned Migrations (Step 1 of P2-1)

> **Status — audited 2026-06-23.** Implemented. Plan B is in place end-to-end: the frozen `_ensure_sqlite_schema`, the no-op `0001_baseline` anchor, and idempotent post-baseline migrations all exist and match this design; subsequent revisions (0002/0003/0004) have already been added beyond the "future" list below.
> _Originally written 2026-06-22. English translation of the Chinese original; now the authoritative version._

> Design date: 2026-06-22
> Scope: migrate schema evolution from the ad-hoc `create_all` + `_ensure_sqlite_schema` mechanism to versioned Alembic migrations.
> Basis: `docs/design/12` P2-1 / 11.2; CLAUDE.md §8; `.claude/rules/database-safety.md`.
> Trigger: the user chose "do P2-1 Alembic first, then route P1-3/P1-4 through migrations."

## 1. Current state (verified at file:line)

| Fact | Location |
|------|----------|
| Production DB = `data/mempas.db` (21MB; `data/` is gitignored; multiple `.bak` files already exist in the directory) | `core/database.py:8-12` |
| Startup table creation = `create_all()` + `_ensure_sqlite_schema()` | `core/database.py:42-45`, `main.py:113` |
| `_ensure_sqlite_schema` is a long chain of v2.5→v4.1 incremental ALTERs / full-table rebuilds | `core/database.py:48-404` |
| **Tests only call `create_all`**, each with its own tmp_path engine, never touching `_ensure_sqlite_schema` and never touching the production DB | `tests/conftest.py:32-63` etc. |
| Models = the schema truth for create_all (the full test suite passing proves the models already contain every column) | `models/*.py` |
| alembic 1.18.4 is already installed, but is not in `requirements.txt` | `pip show` |

## 2. The core fork: the authority relationship between create_all and Alembic

`create_all(checkfirst=True)` builds tables on every startup according to the **current models**. If a table is both in the models (which create_all builds) and in some migration's `op.create_table`, then create_all builds it first → that migration's `CREATE TABLE` on a brand-new DB must conflict. This is the fundamental contradiction of create_all + Alembic coexisting, and one must choose between them:

### Plan A (the textbook end state): Alembic exclusively owns the production schema authority
- Production startup runs only `alembic upgrade head`; create_all / `_ensure_sqlite_schema` are removed from the production path.
- The baseline migration must **exactly replicate the full current schema** (including the partial unique index + CHECK constraints + post-rebuild structure of `bid_alignment_items`).
- Tests still use create_all (a separate path).
- **Risk**: the baseline must be 100% equal to the product of create_all + `_ensure`; autogenerate has blind spots for SQLite partial indexes / CHECK / server_default, which must be filled in by hand and verified item by item. It changes the startup path, with a large regression surface. Going all-in at once is high-risk.

### Plan B (incremental, low-risk, recommended for this step)
- **Keep** create_all + `_ensure_sqlite_schema` as-is (it continues to handle brand-new-DB bootstrap and migrating existing DBs up to the baseline); **freeze** `_ensure_sqlite_schema` (add a comment: no new entries from the baseline onward; all new changes go through Alembic).
- Startup order: `create_all` → `_ensure_sqlite_schema` → (new) `stamp baseline if unversioned` → `upgrade head`.
- The baseline migration's content is a placeholder "representing the current state" (both `upgrade`/`downgrade` are `pass`) — because the current schema of both existing and brand-new DBs is already guaranteed by create_all + `_ensure`, the baseline only needs to **establish a version anchor**; its correctness does not depend on replicating the schema.
- New changes such as P1-3 / P1-4 = new revisions after the baseline; to be compatible with "create_all has already built everything per the models on a brand-new DB," new revisions are written to be **idempotent** (inspect whether the column/table exists before ALTER/CREATE).
  - Existing already-stamped DB: column/table does not exist → the migration runs normally.
  - Brand-new DB: create_all already built it → the migration idempotently skips.
- **Benefit**: the existing production DB moves from "ad-hoc ALTER" to "orderly, reviewable, rollback-able versioned migration"; the already-verified bootstrap is not rewritten; no perfect baseline is needed; the regression surface is minimal.
- **Cost**: migrations need idempotent guards (mildly non-idiomatic); on a brand-new DB create_all is still the actual table-builder, with Alembic mainly doing version bookkeeping on a brand-new DB. This is a safe transitional state toward Plan A; a later batch can switch to A.

> Recommendation: **adopt Plan B for this step**, consistent with "ship fast / stabilize first, clean up later." Plan A is left as an optional later finish, to be switched to once the baseline replication has been thoroughly verified.

## 3. Implementation checklist (Plan B)

### 3.1 Infrastructure (does not touch production data; can be done directly)
1. Add `alembic` to `requirements.txt` (pin the version `==1.18.4`).
2. `apps/api/migrations/`: the `alembic init` output (`env.py` + `script.py.mako` + `versions/`).
3. `alembic.ini` (at the repo root or `apps/api/`): `script_location` points to `apps/api/migrations`; `sqlalchemy.url` is injected by `env.py` from `core.database.DATABASE_URL` (**do not hard-code the path**; comply with database-safety).
4. `env.py`: `target_metadata = Base.metadata`, and import `apps.api.models` (to trigger full-model registration for autogenerate); `render_as_batch=True` (SQLite ALTER needs batch mode).
5. baseline revision (placeholder, `upgrade`/`downgrade` = `pass`, docstring explaining "the version anchor representing the current state of create_all + _ensure").
6. `core/database.py`:
   - Add a freeze comment at the top of `_ensure_sqlite_schema`.
   - Add `_run_alembic_upgrade()`: if there is no `alembic_version` table → `command.stamp(cfg, "base→baseline")`; then `command.upgrade(cfg, "head")`. Use the programmatic API to avoid a shell dependency.
   - Call `_run_alembic_upgrade()` at the end of `init_db()`.
7. Tests unchanged (still create_all); add one unit test: a temporary DB runs `upgrade head` without error, and `alembic_version` lands at head.

> (corrected 2026-06-23: items 1–7 are all implemented. Two faithful-but-minor deviations from the as-written plan: (a) the `alembic.ini` at the repo root deliberately leaves `sqlalchemy.url =` blank and is NOT used by the app at runtime — `_run_alembic_upgrade()` builds the Alembic `Config` programmatically and sets only `script_location`, because `configparser` reads the ini with the OS-locale encoding (GBK on Windows) and chokes on non-ASCII bytes (`core/database.py:70-76`). The ini is retained only for standalone CLI use. (b) `_run_alembic_upgrade()` injects the app engine's own connection via `cfg.attributes["connection"]` inside `engine.begin()` and stamps `"0001_baseline"` specifically; `env.py::run_migrations_online` reuses that injected connection.)

### 3.2 Production-DB transition (touches `data/mempas.db`; follow database-safety)
- Back up → on the backup copy, dry-run `stamp baseline` + `upgrade head` → verify table structure and row-count conservation → user confirms → execute against the production DB.
- For this step the baseline is a no-op; stamp only writes `alembic_version`, with zero data risk; nevertheless follow the process and keep a record.
- **This sub-step is executed separately after 3.1 is complete and self-tested, and requires explicit confirmation.**

### 3.3 Follow-up (unlocked by this design, not implemented in this step)
- P1-3: `ADD COLUMN bid_quote_lines.updated_at` (idempotent revision).
- P1-4: `CREATE TABLE domain_audit_events` (idempotent revision).
- Plan A switchover (remove production create_all, baseline replicates the full schema) — a separate batch.

> (corrected 2026-06-23: 3.3 is now done, but P1-4 landed differently than written here. The actual revisions are `0002_bql_updated_at.py` (P1-3, idempotent ADD COLUMN as planned), `0003_audit_fields.py` (P1-3+P1-4 combined: adds `operation_logs.payload` and `bid_quote_lines.row_type`, idempotent), and `0004_soft_fk.py` (§11.2 soft foreign-key annotation, added 2026-06-23, idempotent via `batch_alter_table`). P1-4 did NOT create a `domain_audit_events` table — instead it added a single `operation_logs.payload` JSON column; see docs/design/14. The Plan A switchover remains undone.)

## 4. Acceptance
- `python -m pytest apps/api/tests -q` matches the baseline (no regression).
- The new migration unit test passes.
- After `upgrade head` on the temporary copy DB, the schema is table-by-table identical to the create_all + _ensure product (compared via `PRAGMA table_info`).
- Production-DB transition: backup exists, `alembic_version=baseline`, business reads/writes are normal, row counts are conserved.

> (corrected 2026-06-23: the migration unit tests exist at `apps/api/tests/test_alembic_migrations.py` and cover more than the single test the plan called for: `test_fresh_db_reaches_head`, `test_init_db_idempotent`, `test_stamp_existing_db_is_structural_noop` (the only structural change allowed on a legacy DB is adding `alembic_version`), `test_bql_updated_at_present_on_fresh_db`, and `test_bql_updated_at_migration_backfills_legacy_db`.)

## 5. Risks and rollback
- Plan B's baseline is a no-op → stamp/upgrade make zero structural changes to an existing DB; rollback = drop the `alembic_version` table to restore the old behavior.
- The new startup `_run_alembic_upgrade` must fail-fast if it throws (it must not silently swallow, or schema drift results).
- The idempotent guard must be based on `inspect`; using try/except to swallow DDL errors and mask a real failure is forbidden.

> (corrected 2026-06-23: the migrations comply with the inspect-based guard rule — `0002`/`0003` use `_has_column` via `sa.inspect`. One narrow exception: `0004_soft_fk._fk_exists` wraps `get_foreign_keys` in a `try/except Exception: pass` to detect a pre-existing constraint; this is a presence check, not DDL-error swallowing, so it stays within the spirit of the rule.)
