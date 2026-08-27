"""docs/design/42 §8 D1 / design/44 F3 — Project.created_by_user_id.

Adds a nullable FK to `users.id` recording who created a project. Nullable
by design: existing projects (created before this column existed) and any
write path without a logged-in user (scripts, migrations) have no creator to
record — left NULL rather than backfilled with a guessed value.

No behavior change here — the write-path restriction (`POST /api/projects`
now requires 管理员) ships in application code alongside this migration, not
in the migration itself.

Revision ID: 0012_project_created_by
Revises: 0011_alignment_round_scope
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_project_created_by"
down_revision = "0011_alignment_round_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in sa.inspect(conn).get_columns("projects")}
    if "created_by_user_id" not in cols:
        op.add_column("projects", sa.Column("created_by_user_id", sa.Integer, nullable=True))
        op.create_index("ix_projects_created_by_user_id", "projects", ["created_by_user_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_created_by_user_id", table_name="projects")
    with op.batch_alter_table("projects", recreate="always") as batch_op:
        batch_op.drop_column("created_by_user_id")
