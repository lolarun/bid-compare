"""brand_tier_approved_canonical

Revision ID: 9f343f645e1f
Revises: 0004_soft_fk
Create Date: 2026-06-26

Add is_approved, canonical_name, alias_of to brand_tiers;
widen category column from VARCHAR(20) to VARCHAR(50);
add ix_brand_tier_canonical index.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '9f343f645e1f'
down_revision = '0004_soft_fk'
branch_labels = None
depends_on = None


def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _idxs(conn) -> set:
    return {row[0] for row in conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index'")
    )}


def upgrade() -> None:
    # Must be idempotent: on a fresh DB create_all already added these columns
    # before _run_alembic_upgrade runs (see database.py _run_alembic_upgrade).
    conn = op.get_bind()
    existing = _cols(conn, 'brand_tiers')
    if 'is_approved' not in existing:
        conn.execute(text(
            "ALTER TABLE brand_tiers ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT 0"
        ))
    if 'canonical_name' not in existing:
        conn.execute(text(
            "ALTER TABLE brand_tiers ADD COLUMN canonical_name VARCHAR(100)"
        ))
    if 'alias_of' not in existing:
        conn.execute(text(
            "ALTER TABLE brand_tiers ADD COLUMN alias_of VARCHAR(100)"
        ))
    # SQLite ignores VARCHAR length; widening category from 20->50 is a no-op.
    if 'ix_brand_tier_canonical' not in _idxs(conn):
        op.create_index('ix_brand_tier_canonical', 'brand_tiers', ['canonical_name', 'category'])


def downgrade() -> None:
    op.drop_index('ix_brand_tier_canonical', table_name='brand_tiers')
    with op.batch_alter_table('brand_tiers') as batch_op:
        batch_op.drop_column('alias_of')
        batch_op.drop_column('canonical_name')
        batch_op.drop_column('is_approved')
