"""§11.2 软外键补正 — 三列裸 Integer 补正式 ForeignKey 约束

Revision ID: 0004_soft_fk
Revises: 0003_audit_fields
Create Date: 2026-06-23

补正以下列（SQLite 要求 batch_alter_table 完成结构变更）：
  bid_alignment_groups.tender_list_session_id   → tender_list_sessions.id
  bid_alignment_items.submission_id             → bid_submissions.id
  alignment_finalizations.project_id            → projects.id
  bid_matrix_versions.project_id               → projects.id

SQLite 默认不强制外键（PRAGMA foreign_keys=OFF）；此迁移的目的是声明式注解，
供 SQLAlchemy 生成正确 DDL 并支持 EXPLAIN / ER 工具。已存在的裸 Integer 行不受影响。
幂等：已存在的 FK 约束视为成功，不报错。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_soft_fk"
down_revision: Union[str, Sequence[str], None] = "0003_audit_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fk_exists(bind, table: str, constraint_name: str | None, columns: list[str]) -> bool:
    """Idempotent guard: return True if a matching FK already exists."""
    try:
        fks = sa.inspect(bind).get_foreign_keys(table)
        for fk in fks:
            if fk["constrained_columns"] == columns:
                return True
    except Exception:
        pass
    return False


def upgrade() -> None:
    bind = op.get_bind()

    # 1. bid_alignment_groups.tender_list_session_id → tender_list_sessions.id
    if not _fk_exists(bind, "bid_alignment_groups", None, ["tender_list_session_id"]):
        with op.batch_alter_table("bid_alignment_groups", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_bag_tender_session", "tender_list_sessions",
                ["tender_list_session_id"], ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index(
                "ix_bag_tender_session", ["tender_list_session_id"],
            )

    # 2. bid_alignment_items.submission_id → bid_submissions.id
    if not _fk_exists(bind, "bid_alignment_items", None, ["submission_id"]):
        with op.batch_alter_table("bid_alignment_items", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_bai_submission", "bid_submissions",
                ["submission_id"], ["id"],
                ondelete="SET NULL",
            )

    # 3. alignment_finalizations.project_id → projects.id
    if not _fk_exists(bind, "alignment_finalizations", None, ["project_id"]):
        with op.batch_alter_table("alignment_finalizations", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_af_project", "projects",
                ["project_id"], ["id"],
                ondelete="SET NULL",
            )

    # 4. bid_matrix_versions.project_id → projects.id
    if not _fk_exists(bind, "bid_matrix_versions", None, ["project_id"]):
        with op.batch_alter_table("bid_matrix_versions", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_bmv_project", "projects",
                ["project_id"], ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    # Dropping FK constraints from SQLite requires full table recreation.
    # This downgrade is provided as a best-effort; it may leave behind orphan constraints.
    with op.batch_alter_table("bid_matrix_versions", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_bmv_project", type_="foreignkey")

    with op.batch_alter_table("alignment_finalizations", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_af_project", type_="foreignkey")

    with op.batch_alter_table("bid_alignment_items", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_bai_submission", type_="foreignkey")

    with op.batch_alter_table("bid_alignment_groups", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_bag_tender_session", type_="foreignkey")
        try:
            batch_op.drop_index("ix_bag_tender_session")
        except Exception:
            pass
