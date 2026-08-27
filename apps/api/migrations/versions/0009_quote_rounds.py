"""docs/design/42 P0 — QuoteRound: multi-round quote collection.

Adds `quote_rounds` and `bid_submissions.round_id`, then backfills a
synthesized round 1 for every (project, category) that already has
submissions but no round yet — see `_backfill_round1` docstring.

This is P0 only: `bid_alignment_groups` gains no round awareness here (see
docs/design/42 §4.1 for why that's deferred to P2, together with the
round_trend feature it's a prerequisite for).

Revision ID: 0009_quote_rounds
Revises: 0008_stage_progress
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import bindparam, text


revision = "0009_quote_rounds"
down_revision = "0008_stage_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # database.init_db creates ORM tables (via create_all) before Alembic runs
    # on fresh SQLite DBs, so this migration must tolerate the table/column
    # already existing — same idempotent-guard pattern as 0007/0008.
    conn = op.get_bind()
    existing_tables = sa.inspect(conn).get_table_names()

    if "quote_rounds" not in existing_tables:
        op.create_table(
            "quote_rounds",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("category", sa.String(50), nullable=False, server_default=""),
            sa.Column("seq", sa.Integer, nullable=False),
            sa.Column("name", sa.String(200), nullable=False, server_default=""),
            sa.Column("stage", sa.String(20), nullable=False, server_default="formal"),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("is_final_basis", sa.Boolean, nullable=False, server_default=sa.text("0")),
            sa.Column(
                "tender_list_session_id", sa.Integer,
                sa.ForeignKey("tender_list_sessions.id"), nullable=True,
            ),
            sa.Column("confirmed_supplier_ids", sa.JSON, nullable=True),
            sa.Column("used_submission_ids", sa.JSON, nullable=True),
            sa.Column("created_by", sa.String(100), nullable=True),
            sa.Column("remark", sa.Text, nullable=False, server_default=""),
            sa.Column("opened_at", sa.DateTime, nullable=True),
            sa.Column("closed_at", sa.DateTime, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )
        op.create_index("ix_quote_rounds_project_id", "quote_rounds", ["project_id"])
        op.create_index("ix_quote_rounds_project_category", "quote_rounds", ["project_id", "category"])
        op.create_index(
            "ix_quote_rounds_project_category_seq", "quote_rounds",
            ["project_id", "category", "seq"], unique=True,
        )

    bs_cols = {c["name"] for c in sa.inspect(conn).get_columns("bid_submissions")}
    if "round_id" not in bs_cols:
        op.add_column("bid_submissions", sa.Column("round_id", sa.Integer, nullable=True))
        op.create_index("ix_bid_submissions_round_id", "bid_submissions", ["round_id"])

    _backfill_round1(conn)


def _backfill_round1(conn) -> None:
    """Synthesize a closed, final-basis round 1 for every (project, category)
    that already has submissions but no round assignment yet.

    A submission's category is not stored on the row itself — it's resolved
    from its BidQuoteLine rows (the most common non-empty
    BidQuoteLine.category, or '' if the submission has none). Where a
    confirmed TenderListSession already exists for that (project, category),
    its used_submission_ids / confirmed_supplier_ids / id are copied onto the
    synthesized round — that is exactly the scope docs/design/42 §3.1 moves
    off TenderListSession onto QuoteRound. Otherwise the round's
    used_submission_ids falls back to "every submission found in this group".

    Runs once in practice: after the first pass every pre-existing submission
    has round_id set, so later app starts see an empty
    `WHERE round_id IS NULL` result and no-op.
    """
    orphans = conn.execute(text(
        "SELECT id, project_id FROM bid_submissions WHERE round_id IS NULL"
    )).fetchall()
    if not orphans:
        return

    orphan_ids = [row[0] for row in orphans]

    # category per submission: majority non-empty BidQuoteLine.category
    cat_rows = conn.execute(
        text(
            "SELECT submission_id, category, COUNT(*) c FROM bid_quote_lines "
            "WHERE submission_id IN :ids GROUP BY submission_id, category"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": orphan_ids},
    ).fetchall()
    best_cat: dict[int, tuple[str, int]] = {}
    for sub_id, category, count in cat_rows:
        category = category or ""
        if not category:
            continue
        prev = best_cat.get(sub_id)
        if prev is None or count > prev[1]:
            best_cat[sub_id] = (category, count)

    # group orphan submissions by (project_id, resolved category)
    groups: dict[tuple[int, str], list[int]] = {}
    for sub_id, project_id in orphans:
        if project_id is None:
            continue
        cat = best_cat.get(sub_id, ("", 0))[0]
        groups.setdefault((project_id, cat), []).append(sub_id)

    for (project_id, category), sub_ids in groups.items():
        existing_round = conn.execute(
            text(
                "SELECT id FROM quote_rounds WHERE project_id = :pid AND category = :cat "
                "ORDER BY seq ASC LIMIT 1"
            ),
            {"pid": project_id, "cat": category},
        ).fetchone()

        if existing_round:
            round_id = existing_round[0]
        else:
            tls = conn.execute(
                text(
                    "SELECT id, used_submission_ids, confirmed_supplier_ids "
                    "FROM tender_list_sessions "
                    "WHERE project_id = :pid AND category = :cat "
                    "AND is_current = 1 AND status = 'confirmed' LIMIT 1"
                ),
                {"pid": project_id, "cat": category},
            ).fetchone()

            if tls and tls[1]:
                used_ids = json.loads(tls[1]) if isinstance(tls[1], str) else tls[1]
                tls_id = tls[0]
            else:
                used_ids = sorted(sub_ids)
                tls_id = tls[0] if tls else None
            confirmed_sids = None
            if tls and tls[2]:
                confirmed_sids = json.loads(tls[2]) if isinstance(tls[2], str) else tls[2]

            # NOT NULL columns are supplied explicitly rather than relied on to
            # fall back to a DDL default: on a fresh DB this table is created by
            # `Base.metadata.create_all` from the ORM model *before* this
            # migration runs (docs/design/13 §2 方案 B), which only carries the
            # model's client-side `default=` — not a `server_default` — so a
            # raw INSERT omitting a NOT NULL column fails there even though the
            # `op.create_table` above (which only fires on a legacy DB) declared
            # a server_default for it.
            result = conn.execute(
                text(
                    "INSERT INTO quote_rounds "
                    "(project_id, category, seq, name, stage, status, is_final_basis, "
                    " tender_list_session_id, used_submission_ids, confirmed_supplier_ids, "
                    " remark, opened_at, closed_at, created_at, updated_at) "
                    "VALUES (:pid, :cat, 1, :name, 'formal', 'closed', 1, "
                    "        :tls_id, :used, :confirmed, "
                    "        '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "pid": project_id, "cat": category, "name": "第一轮",
                    "tls_id": tls_id,
                    "used": json.dumps(used_ids),
                    "confirmed": json.dumps(confirmed_sids) if confirmed_sids is not None else None,
                },
            )
            round_id = result.lastrowid

        conn.execute(
            text("UPDATE bid_submissions SET round_id = :rid WHERE id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"rid": round_id, "ids": sub_ids},
        )


def downgrade() -> None:
    op.drop_index("ix_bid_submissions_round_id", table_name="bid_submissions")
    with op.batch_alter_table("bid_submissions", recreate="always") as batch_op:
        batch_op.drop_column("round_id")
    op.drop_table("quote_rounds")
