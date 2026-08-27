"""docs/design/42 §4.1 (P2) — round-scope BidAlignmentGroup.

Adds `bid_alignment_groups.round_id` + `.anchor_uid`. This is the schema half
of the fix for the latent bug design/42 §4.1 recorded: `import_and_match`'s
wipe-and-rebuild was scoped only to (project_id, category), so opening round 2
and running match against it silently deleted round 1's alignment groups —
the round a decision may already have been made on. The application-side fix
(anchor_match.py scoping the delete by round_id too) ships alongside this
migration; this file only adds the columns and backfills existing rows.

Backfill: every existing `bid_alignment_groups` row predates rounds entirely,
so it is assigned to round 1 of its own (project_id, category) — the same
synthesized round 0009 already created for that scope's submissions. A group
whose (project_id, category) has no round yet (no submissions ever matched
there) is left with round_id=NULL; nothing references it going forward.
`anchor_uid` is left NULL on backfill — it is a cross-round join key with no
useful backward value for a group created before rounds existed, not row
identity, so an empty value is inert rather than wrong.

Revision ID: 0011_alignment_round_scope
Revises: 0010_anchor_uid
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "0011_alignment_round_scope"
down_revision = "0010_anchor_uid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in sa.inspect(conn).get_columns("bid_alignment_groups")}

    if "round_id" not in cols:
        op.add_column("bid_alignment_groups", sa.Column("round_id", sa.Integer, nullable=True))
        op.create_index(
            "ix_bid_alignment_groups_round_id", "bid_alignment_groups", ["round_id"],
        )
    if "anchor_uid" not in cols:
        op.add_column("bid_alignment_groups", sa.Column("anchor_uid", sa.String(64), nullable=True))
        op.create_index(
            "ix_bid_alignment_groups_anchor_uid", "bid_alignment_groups", ["anchor_uid"],
        )

    _backfill_round1(conn)


def _backfill_round1(conn) -> None:
    """Assign every round_id-less group to its (project_id, category)'s round 1.

    Runs once in practice: after the first pass every pre-existing group has
    round_id set, so later app starts see an empty candidate set and no-op.
    """
    orphans = conn.execute(text(
        "SELECT DISTINCT project_id, COALESCE(category, '') FROM bid_alignment_groups "
        "WHERE round_id IS NULL"
    )).fetchall()
    if not orphans:
        return

    for project_id, category in orphans:
        if project_id is None:
            continue
        round_row = conn.execute(
            text(
                "SELECT id FROM quote_rounds WHERE project_id = :pid AND category = :cat "
                "ORDER BY seq ASC LIMIT 1"
            ),
            {"pid": project_id, "cat": category},
        ).fetchone()
        if not round_row:
            # No round ever synthesized for this scope (0009 only creates one
            # where bid_submissions exist) — leave round_id NULL rather than
            # inventing a round with no submissions behind it.
            continue
        conn.execute(
            text(
                "UPDATE bid_alignment_groups SET round_id = :rid "
                "WHERE project_id = :pid AND COALESCE(category, '') = :cat AND round_id IS NULL"
            ),
            {"rid": round_row[0], "pid": project_id, "cat": category},
        )


def downgrade() -> None:
    op.drop_index("ix_bid_alignment_groups_anchor_uid", table_name="bid_alignment_groups")
    op.drop_index("ix_bid_alignment_groups_round_id", table_name="bid_alignment_groups")
    with op.batch_alter_table("bid_alignment_groups", recreate="always") as batch_op:
        batch_op.drop_column("anchor_uid")
        batch_op.drop_column("round_id")
