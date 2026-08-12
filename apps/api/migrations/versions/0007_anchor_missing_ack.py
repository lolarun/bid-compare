"""docs/design/23 — reviewer acknowledgment that an anchor×submission cell
has no quote, and that's expected. Brand-new, additive-only table; no
existing-row migration needed.

Revision ID: 0007_anchor_missing_ack
Revises: 0006_procurement_case
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_anchor_missing_ack"
down_revision = "0006_procurement_case"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # database.init_db creates ORM tables (via create_all) before Alembic runs
    # on fresh SQLite DBs, so this migration must tolerate the table already
    # existing — same pattern as 0005_tender_recommendation_snapshot.
    conn = op.get_bind()
    existing = sa.inspect(conn).get_table_names()
    if "anchor_missing_acks" in existing:
        return

    op.create_table(
        "anchor_missing_acks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column(
            "tender_list_session_id", sa.Integer,
            sa.ForeignKey("tender_list_sessions.id"), nullable=False,
        ),
        sa.Column("anchor_seq", sa.String(20), nullable=False),
        sa.Column(
            "submission_id", sa.Integer,
            sa.ForeignKey("bid_submissions.id"), nullable=False,
        ),
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
    op.create_index("ix_anchor_missing_acks_project_id", "anchor_missing_acks", ["project_id"])


def downgrade() -> None:
    op.drop_table("anchor_missing_acks")
