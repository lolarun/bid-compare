"""P1-3+P1-4: add operation_logs.payload and bid_quote_lines.row_type

Revision ID: 0003_audit_fields
Revises: 0002_bql_updated_at
Create Date: 2026-06-22

两列均为幂等（inspect 先行）：
  operation_logs.payload TEXT  — 结构化域事件 JSON（event_type/identity/before/after/meta）
  bid_quote_lines.row_type VARCHAR(32) — 确认时的行类型快照（quote_line/section_header/…）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_audit_fields"
down_revision: Union[str, Sequence[str], None] = "0002_bql_updated_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind, table: str, col: str) -> bool:
    return col in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "operation_logs", "payload"):
        op.add_column("operation_logs", sa.Column("payload", sa.Text(), nullable=True))

    if not _has_column(bind, "bid_quote_lines", "row_type"):
        op.add_column("bid_quote_lines", sa.Column("row_type", sa.String(32), nullable=True))
        # 存量行全部是 quote_line（非 quote_line 行在 confirm 时被过滤）
        op.execute("UPDATE bid_quote_lines SET row_type = 'quote_line' WHERE row_type IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "operation_logs", "payload"):
        op.drop_column("operation_logs", "payload")
    if _has_column(bind, "bid_quote_lines", "row_type"):
        op.drop_column("bid_quote_lines", "row_type")
