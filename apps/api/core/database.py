"""SQLAlchemy database engine, session, and dependency injection."""

import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session

DB_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "mempas.db"

# 默认仍是生产库 data/mempas.db（24MB 真实数据）。允许 env 覆盖，是因为**在此之前
# 没有任何办法让应用指向别处**：pytest 靠 conftest 的 temp_db 猴补丁绕开，但一旦跑
# 真实 uvicorn 做端到端（前端 E2E 必须如此），上传的测试投标就直接写进生产库，
# 违反 .claude/rules/database-safety.md 第一条。
#
# Alembic 的 env.py 是 `from apps.api.core.database import DATABASE_URL`，所以这里
# 改一处，迁移也跟着指向同一个库——不会出现"表建在 A、数据写到 B"。
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{DB_PATH}"

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    # check_same_thread 是 SQLite 专有的；对其它后端传它会直接报错。
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session per request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Bring the database schema up to date.

    Order (docs/design/13 方案 B):
      1. create_all  — bootstrap a brand-new DB to the full current model schema.
      2. _ensure_sqlite_schema — FROZEN legacy path: migrate pre-Alembic SQLite
         files up to the baseline (no new entries added here going forward).
      3. _run_alembic_upgrade — stamp the baseline anchor if unversioned, then
         apply every versioned migration after it. ALL new schema changes live
         here from now on.
    """
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_schema()
    _run_alembic_upgrade()


def _run_alembic_upgrade():
    """Stamp baseline (if unversioned) then upgrade to head.

    Uses the app engine's own connection so migrations run on the same DB the
    app uses. Migrations added after the baseline MUST be idempotent (inspect
    before ALTER/CREATE), because on a fresh DB create_all has already built the
    full model schema — see docs/design/13 §2 方案 B.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect as _sa_inspect

    # Configure programmatically rather than reading alembic.ini: configparser
    # reads with the OS locale encoding (GBK on Windows), which chokes on any
    # non-ASCII byte in the ini. env.py sources the URL from DATABASE_URL and
    # uses the injected connection, so only script_location is needed here.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))

    # engine.begin() opens a transaction that COMMITS on clean exit. With an
    # injected connection Alembic defers commit to the caller, so we must own
    # the transaction here or the version row never persists.
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        has_version = "alembic_version" in _sa_inspect(conn).get_table_names()
        if not has_version:
            # Existing or freshly-created DB: current schema already == baseline.
            command.stamp(cfg, "0001_baseline")
        command.upgrade(cfg, "head")


def _ensure_sqlite_schema():
    """Apply small additive schema fixes for existing SQLite databases.

    FROZEN (docs/design/13 方案 B): do NOT add new entries here. All schema
    changes from the baseline onward go through versioned Alembic migrations
    under apps/api/migrations/versions/.
    """
    with engine.begin() as conn:
        if engine.dialect.name != "sqlite":
            return

        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(materials)")).fetchall()
        }
        if "status" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE materials "
                    "ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active'"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_materials_status ON materials(status)"))

        job_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(extraction_jobs)")).fetchall()
        }
        if "progress_stage" not in job_columns:
            conn.execute(
                text(
                    "ALTER TABLE extraction_jobs "
                    "ADD COLUMN progress_stage VARCHAR(100) NOT NULL DEFAULT ''"
                )
            )
        if "progress_pct" not in job_columns:
            conn.execute(
                text(
                    "ALTER TABLE extraction_jobs "
                    "ADD COLUMN progress_pct INTEGER NOT NULL DEFAULT 0"
                )
            )

        item_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(bid_alignment_items)")).fetchall()
        }
        if "agg_total" not in item_columns:
            conn.execute(text("ALTER TABLE bid_alignment_items ADD COLUMN agg_total REAL"))
        if "agg_qty" not in item_columns:
            conn.execute(text("ALTER TABLE bid_alignment_items ADD COLUMN agg_qty REAL"))

        # v2.5: anchor linkage columns on bid_alignment_groups
        group_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(bid_alignment_groups)")).fetchall()
        }
        if "tender_list_session_id" not in group_columns:
            conn.execute(text(
                "ALTER TABLE bid_alignment_groups ADD COLUMN tender_list_session_id INTEGER"
            ))
        if "anchor_seq" not in group_columns:
            conn.execute(text(
                "ALTER TABLE bid_alignment_groups ADD COLUMN anchor_seq TEXT"
            ))

        # v2.6: row-level extraction evidence on quotes (for LLM supplier-fill)
        quote_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(quotes)")).fetchall()
        }
        if "extraction_meta_json" not in quote_columns:
            conn.execute(text(
                "ALTER TABLE quotes ADD COLUMN extraction_meta_json JSON"
            ))

        # v2.7: persist supplier scope on TenderListSession (prevents historical-supplier fallback)
        tls_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(tender_list_sessions)")).fetchall()
        }
        if "confirmed_supplier_ids" not in tls_columns:
            conn.execute(text(
                "ALTER TABLE tender_list_sessions ADD COLUMN confirmed_supplier_ids JSON"
            ))

        # v2.8: PDF 招标清单来源 — source_type + 第13页品牌要求/供应商品牌映射
        if "source_type" not in tls_columns:
            conn.execute(text(
                "ALTER TABLE tender_list_sessions "
                "ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'excel'"
            ))
        if "brand_requirement" not in tls_columns:
            conn.execute(text(
                "ALTER TABLE tender_list_sessions ADD COLUMN brand_requirement JSON"
            ))
        if "supplier_brand_map" not in tls_columns:
            conn.execute(text(
                "ALTER TABLE tender_list_sessions ADD COLUMN supplier_brand_map JSON"
            ))

        # v3.0: P0 — Supplier 清洗状态字段
        supplier_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(suppliers)")).fetchall()
        }
        if "merge_status" not in supplier_columns:
            conn.execute(text(
                "ALTER TABLE suppliers "
                "ADD COLUMN merge_status VARCHAR(20) NOT NULL DEFAULT 'active'"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_suppliers_merge_status ON suppliers(merge_status)"
            ))
        if "merged_into_supplier_id" not in supplier_columns:
            conn.execute(text(
                "ALTER TABLE suppliers "
                "ADD COLUMN merged_into_supplier_id INTEGER REFERENCES suppliers(id)"
            ))

        # v3.0: P0 — bid_alignment_items 12步重建
        #
        # 目标：添加 bid_quote_line_id 列 + CHECK 约束（两列只能一个非空）。
        # SQLite 不支持 ALTER COLUMN / ADD CONSTRAINT，必须全表重建。
        #
        # 触发条件：bid_quote_line_id 列不在现有表中。
        # 新库路径：create_all() 已建正确 schema（含 bid_quote_line_id），仅需创建偏部唯一索引。
        # 注意：PRAGMA foreign_keys = OFF 在事务内可能无效，但 DDL 操作本身不触发 FK 校验，
        #       故仍可安全执行；INSERT...SELECT 只复制已存在的合法数据，不会违反 FK 约束。
        p0_item_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(bid_alignment_items)")).fetchall()
        }
        needs_rebuild = "bid_quote_line_id" not in p0_item_cols

        if needs_rebuild:
            n_before = conn.execute(text("SELECT COUNT(*) FROM bid_alignment_items")).scalar()

            conn.execute(text("PRAGMA foreign_keys = OFF"))
            try:
                conn.execute(text("""
                    CREATE TABLE bid_alignment_items_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id INTEGER NOT NULL
                            REFERENCES bid_alignment_groups(id) ON DELETE CASCADE,
                        quote_id INTEGER REFERENCES quotes(id),
                        bid_quote_line_id INTEGER REFERENCES bid_quote_lines(id),
                        supplier_id INTEGER REFERENCES suppliers(id),
                        action VARCHAR(20) DEFAULT 'align',
                        spec_note VARCHAR(500) DEFAULT '',
                        agg_total REAL,
                        agg_qty REAL,
                        name_note VARCHAR(500) DEFAULT '',
                        created_at DATETIME,
                        CHECK (
                            (quote_id IS NOT NULL AND bid_quote_line_id IS NULL) OR
                            (quote_id IS NULL AND bid_quote_line_id IS NOT NULL)
                        )
                    )
                """))

                # 动态列集：兼容旧库中 name_note / created_at 可能不存在的情况
                base_cols = ["id", "group_id", "quote_id", "supplier_id", "action", "spec_note"]
                opt_cols = [c for c in ("agg_total", "agg_qty", "name_note", "created_at")
                            if c in p0_item_cols]
                src_cols = base_cols + opt_cols
                col_list = ", ".join(src_cols + ["bid_quote_line_id"])
                sel_list = ", ".join(src_cols + ["NULL AS bid_quote_line_id"])

                conn.execute(text(
                    f"INSERT INTO bid_alignment_items_new ({col_list}) "
                    f"SELECT {sel_list} FROM bid_alignment_items"
                ))

                n_copy = conn.execute(
                    text("SELECT COUNT(*) FROM bid_alignment_items_new")
                ).scalar()
                if n_copy != n_before:
                    raise RuntimeError(
                        f"bid_alignment_items rebuild 行数不一致: "
                        f"预期 {n_before}，实际复制 {n_copy}"
                    )

                conn.execute(text("DROP TABLE bid_alignment_items"))
                conn.execute(text(
                    "ALTER TABLE bid_alignment_items_new RENAME TO bid_alignment_items"
                ))

                # 偏部唯一索引（两路径互斥）
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_align_item_group_quote "
                    "ON bid_alignment_items(group_id, quote_id) WHERE quote_id IS NOT NULL"
                ))
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_align_item_group_bql "
                    "ON bid_alignment_items(group_id, bid_quote_line_id) "
                    "WHERE bid_quote_line_id IS NOT NULL"
                ))
                # 普通查询索引
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bai_group_id ON bid_alignment_items(group_id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bai_quote_id ON bid_alignment_items(quote_id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bai_bql_id "
                    "ON bid_alignment_items(bid_quote_line_id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bai_supplier_id "
                    "ON bid_alignment_items(supplier_id)"
                ))
            finally:
                conn.execute(text("PRAGMA foreign_keys = ON"))

            # 步骤11: FK 完整性校验
            violations = conn.execute(
                text("PRAGMA foreign_key_check(bid_alignment_items)")
            ).fetchall()
            if violations:
                raise RuntimeError(
                    f"bid_alignment_items 重建后存在 FK 违规: {violations[:5]}"
                )

            # 步骤12: 最终行数守恒校验
            n_final = conn.execute(text("SELECT COUNT(*) FROM bid_alignment_items")).scalar()
            if n_final != n_before:
                raise RuntimeError(
                    f"bid_alignment_items 行数守恒失败: {n_before} → {n_final}"
                )

        else:
            # 新库 / 已完成迁移的库：确保偏部唯一索引存在
            # （create_all() 不会为偏部索引执行 CREATE INDEX，需在此补建）
            idx_names = {
                row[0] for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='index'")
                ).fetchall()
            }
            if "ix_align_item_group_quote" not in idx_names:
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_align_item_group_quote "
                    "ON bid_alignment_items(group_id, quote_id) WHERE quote_id IS NOT NULL"
                ))
            if "ix_align_item_group_bql" not in idx_names:
                conn.execute(text(
                    "CREATE UNIQUE INDEX ix_align_item_group_bql "
                    "ON bid_alignment_items(group_id, bid_quote_line_id) "
                    "WHERE bid_quote_line_id IS NOT NULL"
                ))

        # v3.1: used_submission_ids on TenderListSession — shared batch reference
        tls_cols_v31 = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(tender_list_sessions)")
            ).fetchall()
        }
        if "used_submission_ids" not in tls_cols_v31:
            conn.execute(text(
                "ALTER TABLE tender_list_sessions ADD COLUMN used_submission_ids JSON"
            ))

        # v4.0: 弱关联 — bid_submissions.supplier_id 改为 nullable（移除 NOT NULL 约束）
        # SQLite 不支持 ALTER COLUMN，需全表重建。
        bs_cols_info = conn.execute(text("PRAGMA table_info(bid_submissions)")).fetchall()
        bs_cols = {row[1] for row in bs_cols_info}
        # Detect: if supplier_id has notnull=1, rebuild is needed
        bs_sid_notnull = next(
            (row[3] for row in bs_cols_info if row[1] == "supplier_id"), 1
        )
        if bs_sid_notnull:  # 1 = NOT NULL; 0 = NULL ok
            n_bs_before = conn.execute(text("SELECT COUNT(*) FROM bid_submissions")).scalar()
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            try:
                conn.execute(text("""
                    CREATE TABLE bid_submissions_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id VARCHAR(36) NOT NULL REFERENCES extraction_jobs(id),
                        supplier_id INTEGER REFERENCES suppliers(id),
                        supplier_raw_name VARCHAR(200) NOT NULL DEFAULT '',
                        project_id INTEGER REFERENCES projects(id),
                        batch_id VARCHAR(100) NOT NULL UNIQUE,
                        status VARCHAR(30) NOT NULL DEFAULT 'pending',
                        bid_status VARCHAR(30) NOT NULL DEFAULT '',
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                """))
                opt_bs_cols = [c for c in ("bid_status",) if c in bs_cols]
                base_bs = ["id", "job_id", "supplier_id", "supplier_raw_name",
                           "project_id", "batch_id", "status", "created_at", "updated_at"]
                all_bs = base_bs + [c for c in opt_bs_cols if c not in base_bs]
                col_list = ", ".join(all_bs)
                conn.execute(text(
                    f"INSERT INTO bid_submissions_new ({col_list}) "
                    f"SELECT {col_list} FROM bid_submissions"
                ))
                n_bs_after = conn.execute(
                    text("SELECT COUNT(*) FROM bid_submissions_new")
                ).scalar()
                if n_bs_after != n_bs_before:
                    raise RuntimeError(
                        f"bid_submissions 重建行数不一致: {n_bs_before} → {n_bs_after}"
                    )
                conn.execute(text("DROP TABLE bid_submissions"))
                conn.execute(text(
                    "ALTER TABLE bid_submissions_new RENAME TO bid_submissions"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bid_submissions_job_id "
                    "ON bid_submissions(job_id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bid_submissions_supplier_id "
                    "ON bid_submissions(supplier_id)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_bid_submissions_project_id "
                    "ON bid_submissions(project_id)"
                ))
            finally:
                conn.execute(text("PRAGMA foreign_keys = ON"))

        # v4.0: submission_id column on bid_alignment_items
        bai_cols_v40 = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(bid_alignment_items)")
            ).fetchall()
        }
        if "submission_id" not in bai_cols_v40:
            conn.execute(text(
                "ALTER TABLE bid_alignment_items ADD COLUMN submission_id INTEGER"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_bai_submission_id "
                "ON bid_alignment_items(submission_id)"
            ))

        # v4.1: ExtractionJob.lifecycle — job 自带业务生命周期（active/confirmed/removed），
        # compare-state 据此判定在途，不再反查 bid_submissions。
        job_cols_v41 = {
            row[1] for row in conn.execute(
                text("PRAGMA table_info(extraction_jobs)")
            ).fetchall()
        }
        if "lifecycle" not in job_cols_v41:
            conn.execute(text(
                "ALTER TABLE extraction_jobs "
                "ADD COLUMN lifecycle VARCHAR(16) NOT NULL DEFAULT 'active'"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_extraction_jobs_lifecycle "
                "ON extraction_jobs(lifecycle)"
            ))
            # 回填：job_id 唯一对应至多一条 submission（batch_id=BID-{job_id}）。
            #   active submission   → job confirmed
            #   superseded/rejected → job removed
            #   无 submission       → 保持 active
            conn.execute(text(
                "UPDATE extraction_jobs SET lifecycle='confirmed' WHERE id IN ("
                "  SELECT job_id FROM bid_submissions "
                "  WHERE job_id IS NOT NULL AND status NOT IN ('superseded','rejected'))"
            ))
            conn.execute(text(
                "UPDATE extraction_jobs SET lifecycle='removed' WHERE id IN ("
                "  SELECT job_id FROM bid_submissions "
                "  WHERE job_id IS NOT NULL AND status IN ('superseded','rejected'))"
            ))
