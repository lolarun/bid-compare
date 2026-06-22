"""Alembic migration wiring tests (docs/design/13 方案 B).

Verifies init_db() stamps the baseline and reaches head on a fresh DB, that
re-running is idempotent, and that stamping an existing (pre-Alembic) DB makes
no structural changes beyond adding the alembic_version table.
"""
from __future__ import annotations

import shutil

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
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path

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
