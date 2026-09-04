"""Alembic migration wiring tests (docs/design/13 方案 B).

Verifies init_db() stamps the baseline and reaches head on a fresh DB, that
re-running is idempotent, and that stamping an existing (pre-Alembic) DB makes
no structural changes beyond adding the alembic_version table.
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import apps.api.core.database as db_mod
import apps.api.models  # noqa: F401 — register all models


def _schema_snapshot(engine):
    insp = inspect(engine)
    snap = {}
    for t in sorted(insp.get_table_names()):
        if t == "alembic_version":
            continue
        cols = sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(t))
        snap[t] = cols
    return snap


def _point_engine(monkeypatch, db_path):
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", SessionLocal)
    return engine


def _head_revision():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migrations_dir = Path(db_mod.__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _version(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_fresh_db_reaches_head(tmp_path, monkeypatch):
    engine = _point_engine(monkeypatch, tmp_path / "fresh.db")
    db_mod.init_db()
    assert "alembic_version" in inspect(engine).get_table_names()
    assert _version(engine) == _head_revision()
    engine.dispose()


def test_init_db_idempotent(tmp_path, monkeypatch):
    engine = _point_engine(monkeypatch, tmp_path / "fresh.db")
    db_mod.init_db()
    before = _schema_snapshot(engine)
    db_mod.init_db()  # second run must not error or change schema
    assert _schema_snapshot(engine) == before
    assert _version(engine) == _head_revision()
    engine.dispose()


def test_stamp_existing_db_is_structural_noop(tmp_path, monkeypatch):
    """Build a DB the legacy way (create_all only, no alembic_version), then run
    init_db: the only structural change allowed is adding alembic_version."""
    db_path = tmp_path / "legacy.db"
    legacy_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    db_mod.Base.metadata.create_all(bind=legacy_engine)
    before = _schema_snapshot(legacy_engine)
    legacy_engine.dispose()

    engine = _point_engine(monkeypatch, db_path)
    db_mod.init_db()
    after = _schema_snapshot(engine)

    assert after == before, "init_db altered existing schema beyond alembic_version"
    assert _version(engine) is not None
    engine.dispose()


def test_bql_updated_at_present_on_fresh_db(tmp_path, monkeypatch):
    engine = _point_engine(monkeypatch, tmp_path / "fresh.db")
    db_mod.init_db()
    cols = {c["name"] for c in inspect(engine).get_columns("bid_quote_lines")}
    assert "updated_at" in cols
    engine.dispose()


def test_bql_updated_at_migration_backfills_legacy_db(tmp_path, monkeypatch):
    """A pre-P1-3 DB (no updated_at) gets the column added and backfilled to
    created_at when init_db runs migration 0002."""
    from apps.api.models import BidQuoteLine

    db_path = tmp_path / "legacy.db"
    legacy_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    db_mod.Base.metadata.create_all(bind=legacy_engine)
    Session = sessionmaker(bind=legacy_engine)
    s = Session()
    s.add(BidQuoteLine(submission_id=1, raw_name="X"))
    s.commit()
    s.close()
    with legacy_engine.begin() as c:
        c.execute(text("ALTER TABLE bid_quote_lines DROP COLUMN updated_at"))
    cols_before = {c["name"] for c in inspect(legacy_engine).get_columns("bid_quote_lines")}
    assert "updated_at" not in cols_before
    legacy_engine.dispose()

    engine = _point_engine(monkeypatch, db_path)
    db_mod.init_db()

    cols_after = {c["name"] for c in inspect(engine).get_columns("bid_quote_lines")}
    assert "updated_at" in cols_after
    with engine.connect() as c:
        created, updated = c.execute(
            text("SELECT created_at, updated_at FROM bid_quote_lines")
        ).fetchone()
    assert created == updated, "updated_at not backfilled from created_at"
    engine.dispose()
