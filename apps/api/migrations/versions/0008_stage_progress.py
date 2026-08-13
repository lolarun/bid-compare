"""docs/design/24 B2 — extraction_jobs.stage_current / stage_total.

Additive-only nullable Integer columns. SQLite supports ADD COLUMN natively
for nullable columns (no batch_alter_table/recreate needed, unlike FK/
constraint changes in 0004).

Revision ID: 0008_stage_progress
Revises: 0007_anchor_missing_ack
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_stage_progress"
down_revision = "0007_anchor_missing_ack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # database.init_db creates ORM tables (via create_all) before Alembic runs
    # on fresh SQLite DBs, so this migration must tolerate the columns already
    # existing — same idempotent-guard pattern as 0005/0007.
    conn = op.get_bind()
    existing_cols = {c["name"] for c in sa.inspect(conn).get_columns("extraction_jobs")}

    if "stage_current" not in existing_cols:
        op.add_column("extraction_jobs", sa.Column("stage_current", sa.Integer, nullable=True))
    if "stage_total" not in existing_cols:
        op.add_column("extraction_jobs", sa.Column("stage_total", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("extraction_jobs", recreate="always") as batch_op:
        batch_op.drop_column("stage_total")
        batch_op.drop_column("stage_current")
