"""P1-3: add bid_quote_lines.updated_at (row-level audit timestamp)

Revision ID: 0002_bql_updated_at
Revises: 0001_baseline
Create Date: 2026-06-22

幂等（docs/design/13 方案 B §3.3）：全新库由 create_all 已按模型建好该列，
本迁移须先 inspect 再 ADD，避免重复添加。存量库（baseline stamp 后）该列
不存在 → 添加并回填为 created_at。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_bql_updated_at"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "bid_quote_lines"
_COL = "updated_at"


def _has_column(bind, table: str, col: str) -> bool:
    insp = sa.inspect(bind)
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, _TABLE, _COL):
        return  # fresh DB: create_all already built it
    op.add_column(_TABLE, sa.Column(_COL, sa.DateTime(), nullable=True))
    # Backfill existing rows so updated_at is meaningful from day one.
    op.execute(
        f"UPDATE {_TABLE} SET {_COL} = created_at WHERE {_COL} IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, _TABLE, _COL):
        return
    op.drop_column(_TABLE, _COL)
