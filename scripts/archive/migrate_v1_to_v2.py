"""Database migration: v1 → v2.1

Applies incremental schema changes to an existing MEMPAS SQLite database.
Safe to run multiple times — each change checks if it already exists.

Usage:
    python scripts/migrate_v1_to_v2.py [--db data/mempas.db] [--dry-run]

Changes applied:
  1. quotes: add brand_tier, batch_id, baseline_type, bid_status columns
  2. materials: add ref_price_reasonable_low, recommended_brands, supplier_count, status
  3. suppliers: add is_new, supplier_type columns
  4. extraction_jobs: add progress_stage, progress_pct columns
  5. Create new tables: users, operation_logs, bid_invitations, tender_documents,
     brand_tiers, analysis_config, extraction_jobs, bid_alignment_groups/items
"""

import argparse
import sqlite3
import sys
from datetime import datetime


def get_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return set of column names for a table."""
    try:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {c[1] for c in cols}
    except Exception:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    r = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return r[0] > 0


def migrate(db_path: str, dry_run: bool = False) -> list[str]:
    """Run all migrations. Returns list of applied changes."""
    conn = sqlite3.connect(db_path)
    changes: list[str] = []

    def exe(sql: str, desc: str):
        changes.append(desc)
        print(f"  {'[DRY RUN] ' if dry_run else ''}  {desc}")
        if not dry_run:
            conn.execute(sql)

    print(f"Migrating {db_path} ...")
    print(f"{'DRY RUN — no changes will be made' if dry_run else 'Applying changes ...'}\n")

    # ── 1. quotes table additions ──────────────────────────────────────────
    if table_exists(conn, "quotes"):
        cols = get_columns(conn, "quotes")
        if "brand_tier" not in cols:
            exe("ALTER TABLE quotes ADD COLUMN brand_tier VARCHAR(20) DEFAULT ''",
                "quotes: add brand_tier")
        if "batch_id" not in cols:
            exe("ALTER TABLE quotes ADD COLUMN batch_id VARCHAR(50) DEFAULT ''",
                "quotes: add batch_id")
        if "baseline_type" not in cols:
            exe("ALTER TABLE quotes ADD COLUMN baseline_type VARCHAR(20) DEFAULT 'median'",
                "quotes: add baseline_type")
        if "bid_status" not in cols:
            exe("ALTER TABLE quotes ADD COLUMN bid_status VARCHAR(20) DEFAULT ''",
                "quotes: add bid_status")

    # ── 2. materials table additions ──────────────────────────────────────
    if table_exists(conn, "materials"):
        cols = get_columns(conn, "materials")
        if "ref_price_reasonable_low" not in cols:
            exe("ALTER TABLE materials ADD COLUMN ref_price_reasonable_low REAL",
                "materials: add ref_price_reasonable_low")
        if "recommended_brands" not in cols:
            exe("ALTER TABLE materials ADD COLUMN recommended_brands TEXT DEFAULT ''",
                "materials: add recommended_brands")
        if "supplier_count" not in cols:
            exe("ALTER TABLE materials ADD COLUMN supplier_count INTEGER DEFAULT 0",
                "materials: add supplier_count")
        if "status" not in cols:
            exe("ALTER TABLE materials ADD COLUMN status VARCHAR(20) DEFAULT 'active'",
                "materials: add status")

    # ── 3. suppliers table additions ──────────────────────────────────────
    if table_exists(conn, "suppliers"):
        cols = get_columns(conn, "suppliers")
        if "is_new" not in cols:
            exe("ALTER TABLE suppliers ADD COLUMN is_new BOOLEAN DEFAULT 0",
                "suppliers: add is_new")
        if "supplier_type" not in cols:
            exe("ALTER TABLE suppliers ADD COLUMN supplier_type VARCHAR(20) DEFAULT ''",
                "suppliers: add supplier_type")

    # ── 4. extraction_jobs table additions ────────────────────────────────
    if table_exists(conn, "extraction_jobs"):
        cols = get_columns(conn, "extraction_jobs")
        if "progress_stage" not in cols:
            exe("ALTER TABLE extraction_jobs ADD COLUMN progress_stage VARCHAR(30) DEFAULT ''",
                "extraction_jobs: add progress_stage")
        if "progress_pct" not in cols:
            exe("ALTER TABLE extraction_jobs ADD COLUMN progress_pct REAL DEFAULT 0",
                "extraction_jobs: add progress_pct")

    # ── 5. Create new tables if missing ───────────────────────────────────

    if not table_exists(conn, "users"):
        exe("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(200) NOT NULL DEFAULT '',
            password_salt VARCHAR(50) NOT NULL DEFAULT '',
            nickname VARCHAR(50) DEFAULT '',
            role VARCHAR(20) DEFAULT '查看者',
            email VARCHAR(100) DEFAULT '',
            phone VARCHAR(20) DEFAULT '',
            status VARCHAR(10) DEFAULT '启用',
            last_login DATETIME,
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: users")

    if not table_exists(conn, "operation_logs"):
        exe("""CREATE TABLE operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user VARCHAR(50) DEFAULT '',
            module VARCHAR(50) DEFAULT '',
            action VARCHAR(50) DEFAULT '',
            target VARCHAR(200) DEFAULT '',
            result VARCHAR(10) DEFAULT '成功',
            remark TEXT DEFAULT '',
            created_at DATETIME
        )""", "create table: operation_logs")

    if not table_exists(conn, "brand_tiers"):
        exe("""CREATE TABLE brand_tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_name VARCHAR(100) NOT NULL,
            tier VARCHAR(20) NOT NULL,
            category VARCHAR(50),
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: brand_tiers")

    if not table_exists(conn, "analysis_config"):
        exe("""CREATE TABLE analysis_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key VARCHAR(50) UNIQUE NOT NULL,
            value TEXT DEFAULT '{}',
            description VARCHAR(200) DEFAULT '',
            updated_at DATETIME
        )""", "create table: analysis_config")

    if not table_exists(conn, "extraction_jobs"):
        exe("""CREATE TABLE extraction_jobs (
            id VARCHAR(36) PRIMARY KEY,
            type VARCHAR(10) NOT NULL,
            status VARCHAR(10) DEFAULT 'pending',
            filename VARCHAR(200) DEFAULT '',
            file_hash VARCHAR(64) DEFAULT '',
            file_size INTEGER DEFAULT 0,
            file_path VARCHAR(500) DEFAULT '',
            mime_type VARCHAR(50) DEFAULT '',
            context TEXT DEFAULT '{}',
            result TEXT,
            error TEXT DEFAULT '',
            confidence REAL,
            provider VARCHAR(50) DEFAULT '',
            tokens_used INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            progress_stage VARCHAR(30) DEFAULT '',
            progress_pct REAL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: extraction_jobs")

    if not table_exists(conn, "tender_documents"):
        exe("""CREATE TABLE tender_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id VARCHAR(36),
            project_id INTEGER,
            project_name VARCHAR(200) DEFAULT '',
            project_code VARCHAR(50) DEFAULT '',
            tender_date VARCHAR(20) DEFAULT '',
            deadline VARCHAR(20) DEFAULT '',
            items TEXT DEFAULT '[]',
            status VARCHAR(20) DEFAULT 'draft',
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: tender_documents")

    if not table_exists(conn, "bid_invitations"):
        exe("""CREATE TABLE bid_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tender_id INTEGER,
            supplier_id INTEGER,
            score REAL,
            rank INTEGER,
            reason TEXT DEFAULT '',
            status VARCHAR(20) DEFAULT 'pending',
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: bid_invitations")

    if not table_exists(conn, "bid_alignment_groups"):
        exe("""CREATE TABLE bid_alignment_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            category VARCHAR(50) DEFAULT '',
            suggested_name VARCHAR(200) DEFAULT '',
            suggested_spec VARCHAR(500) DEFAULT '',
            suggested_unit VARCHAR(20) DEFAULT '',
            suggested_qty REAL,
            confidence REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            status VARCHAR(20) DEFAULT 'confirmed',
            created_at DATETIME,
            updated_at DATETIME
        )""", "create table: bid_alignment_groups")

    if not table_exists(conn, "bid_alignment_items"):
        exe("""CREATE TABLE bid_alignment_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES bid_alignment_groups(id) ON DELETE CASCADE,
            quote_id INTEGER NOT NULL REFERENCES quotes(id),
            supplier_id INTEGER,
            action VARCHAR(20) DEFAULT 'align',
            spec_note VARCHAR(500) DEFAULT '',
            name_note VARCHAR(500) DEFAULT '',
            created_at DATETIME
        )""", "create table: bid_alignment_items")

    # ── Commit ────────────────────────────────────────────────────────────
    if not dry_run:
        conn.commit()
    conn.close()

    if not changes:
        print("  No changes needed — database is up to date.")
    else:
        print(f"\n{'Would apply' if dry_run else 'Applied'} {len(changes)} change(s).")
    return changes


def main():
    parser = argparse.ArgumentParser(description="MEMPAS DB migration v1 → v2.1")
    parser.add_argument("--db", default="data/mempas.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without applying")
    args = parser.parse_args()

    migrate(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
