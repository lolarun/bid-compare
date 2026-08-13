"""Tombstone: ProcurementCase schema was introduced here, then abandoned.

The `ProcurementCase` ORM model and its three foreign keys were removed while the
closed-roster design (docs/design/18) is still undecided. This revision is kept as
a NO-OP rather than deleted, because databases are already stamped at it — removing
the file makes `alembic current` unresolvable and blocks application startup.

Effects on already-stamped databases: the `procurement_cases` table and the
`procurement_case_id` columns remain in place, unreferenced and inert. No code
reads them. Fresh databases never get them.

If docs/design/18 is adopted, add a NEW idempotent revision that creates the
schema — do not resurrect this one.

Revision ID: 0006_procurement_case
Revises: 0005_tender_recommendation_snapshot
"""

revision = "0006_procurement_case"
down_revision = "0005_tender_recommendation_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op — see module docstring."""


def downgrade() -> None:
    """No-op — see module docstring."""
