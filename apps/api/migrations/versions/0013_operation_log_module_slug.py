"""Normalise OperationLog.module to the canonical lowercase-slug vocabulary.

`operation_logs.module` was written in two conventions at once: the structured
domain-event path (services/audit.py) wrote the slug "bid-compare", while the
login and user-management paths wrote Chinese display names ("系统",
"用户管理"). The column is a filter key, not display copy, so the slug side
wins and the UI label now comes from `core.enums.LOG_MODULE_LABELS`.

The legacy map is inlined rather than imported from `core.enums` so this
revision keeps rewriting the same historical values even if the registry
changes later.

Revision ID: 0013_operation_log_module_slug
Revises: 0012_project_created_by
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_operation_log_module_slug"
down_revision = "0012_project_created_by"
branch_labels = None
depends_on = None

# legacy stored value → canonical slug. Only the two values production code
# actually wrote are listed; "bid-compare" rows were already canonical.
_LEGACY_TO_SLUG = {
    "系统": "system",
    "用户管理": "user-management",
}


def _rename(conn, mapping: dict[str, str]) -> None:
    """Rewrite module values in place, preserving the total row count."""
    if not sa.inspect(conn).has_table("operation_logs"):
        return
    before = conn.execute(sa.text("SELECT COUNT(*) FROM operation_logs")).scalar_one()
    for old, new in mapping.items():
        conn.execute(
            sa.text("UPDATE operation_logs SET module = :new WHERE module = :old"),
            {"new": new, "old": old},
        )
    after = conn.execute(sa.text("SELECT COUNT(*) FROM operation_logs")).scalar_one()
    if before != after:  # pragma: no cover — UPDATE cannot change row count
        raise RuntimeError(f"operation_logs row count changed: {before} -> {after}")


def upgrade() -> None:
    # Idempotent: re-running finds no legacy values left and is a no-op.
    _rename(op.get_bind(), _LEGACY_TO_SLUG)


def downgrade() -> None:
    # Restores the pre-upgrade state exactly: "bid-compare" was already the
    # stored value before this revision and is left alone.
    _rename(op.get_bind(), {v: k for k, v in _LEGACY_TO_SLUG.items()})
