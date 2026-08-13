"""Persist the evidence used to create an invitation list.

Revision ID: 0005_tender_recommendation_snapshot
Revises: 9f343f645e1f
"""

from alembic import op
from sqlalchemy import text


revision = "0005_tender_recommendation_snapshot"
down_revision = "9f343f645e1f"
branch_labels = None
depends_on = None


def _cols(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    # database.init_db creates ORM tables before Alembic on fresh SQLite DBs,
    # so this migration must also tolerate an already-present column.
    conn = op.get_bind()
    if "recommendation_snapshot" not in _cols(conn, "tender_documents"):
        conn.execute(text(
            "ALTER TABLE tender_documents "
            "ADD COLUMN recommendation_snapshot JSON"
        ))


def downgrade() -> None:
    with op.batch_alter_table("tender_documents") as batch_op:
        batch_op.drop_column("recommendation_snapshot")
