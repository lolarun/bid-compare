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
