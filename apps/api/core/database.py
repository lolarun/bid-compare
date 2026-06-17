"""SQLAlchemy database engine, session, and dependency injection."""

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session

DB_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "mempas.db"

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
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
    """Create all tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_schema()


def _ensure_sqlite_schema():
    """Apply small additive schema fixes for existing SQLite databases."""
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
