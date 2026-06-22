"""baseline — version anchor for the pre-Alembic schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-22

方案 B（docs/design/13）：本 baseline 是 **no-op 版本锚点**，不复刻 schema。

理由：存量库（data/mempas.db）与全新库的当前 schema 已分别由
  - 存量：历史 _ensure_sqlite_schema 的 v2.5→v4.1 ALTER 链
  - 全新：create_all() 按模型直接建全表
保证。baseline 只负责让 alembic_version 有一个起点，使其后的
P1-3 / P1-4 等 versioned migration 能有序叠加。

存量库通过 `alembic stamp 0001_baseline` 标记到此版本（不执行任何 DDL）；
全新库由 init_db() 先 create_all 建表、再 stamp 到此版本。
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: schema at this point is established by create_all/_ensure_sqlite_schema."""
    pass


def downgrade() -> None:
    """No-op anchor — nothing to undo."""
    pass
