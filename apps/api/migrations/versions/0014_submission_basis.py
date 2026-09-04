"""份级口径（可比性基准）落点 —— P1，见 .claude/plans/comparability-basis-dimensions.md

真实招标里决定"能不能比"的不止含税/不含税：母线第一轮四家中一家报「不含安装」
827,034、其余三家「含安装」，四家铜价基准还各不相同（77540/76600/77470/77680，
二轮才统一到 73410）。把这些数放在一起排序就是静默混比。

一行一个 (submission, dim)：口径要按维度查询、要能逐维加约束，所以是独立表而
不是 BidSubmission 上的 JSON 列（用户 2026-09-03 决策）。

Revision ID: 0014_submission_basis
Revises: 0013_operation_log_module_slug
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_submission_basis"
down_revision = "0013_operation_log_module_slug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # database.init_db 在全新 SQLite 上先跑 create_all 再跑 Alembic，所以这里
    # 必须容忍表已存在——与 0007/0008/0009 同一道幂等守卫。
    conn = op.get_bind()
    if "submission_basis" in sa.inspect(conn).get_table_names():
        return

    op.create_table(
        "submission_basis",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "submission_id", sa.Integer,
            sa.ForeignKey("bid_submissions.id"), nullable=False,
        ),
        sa.Column("dim", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="extracted"),
        # not_present / extraction_failed 时为 NULL——不用空字典冒充"没有"
        sa.Column("value", sa.JSON, nullable=True),
        sa.Column("raw_text", sa.Text, nullable=False, server_default=""),
        sa.Column("source_ref", sa.JSON, nullable=True),
        sa.Column("extracted_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("confirmed_by", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        # 一份报价一个维度只有一个当前取值；改值是 UPDATE，历史留在操作日志
        sa.UniqueConstraint("submission_id", "dim", name="uq_submission_basis_dim"),
    )
    op.create_index("ix_submission_basis_submission_id", "submission_basis", ["submission_id"])
    op.create_index("ix_submission_basis_dim", "submission_basis", ["dim"])
    op.create_index("ix_submission_basis_status", "submission_basis", ["status"])


def downgrade() -> None:
    conn = op.get_bind()
    if "submission_basis" not in sa.inspect(conn).get_table_names():
        return
    op.drop_index("ix_submission_basis_status", table_name="submission_basis")
    op.drop_index("ix_submission_basis_dim", table_name="submission_basis")
    op.drop_index("ix_submission_basis_submission_id", table_name="submission_basis")
    op.drop_table("submission_basis")
