"""Shared pytest fixtures for backend tests.

Each test runs against an isolated SQLite DB under tmp_path so the production
mempas.db is never touched.

Extraction jobs run INLINE during tests (EXTRACTION_MODE=inline auto-set
below) so TestClient responses include completed job state. Production uses
the thread-pool path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# Force inline extraction for the whole test session so legacy tests that
# expect BackgroundTasks-style synchronous completion still work without
# adding polling everywhere.
os.environ.setdefault("EXTRACTION_MODE", "inline")


# Marker for end-to-end tests that need a real DASHSCOPE_API_KEY.
def pytest_configure(config):  # pragma: no cover
    config.addinivalue_line("markers", "e2e: end-to-end tests using real Qwen-VL API")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create a fresh SQLite DB per test, isolated under tmp_path.

    Yields (engine, SessionLocal) plus monkey-patches apps.api.core.database
    so any code that imports `engine` or `SessionLocal` after this fixture
    runs sees the temp one.
    """
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Late imports so the patch lands before Base.metadata.create_all
    from apps.api.core import database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", SessionLocal)

    # Re-bind Base so create_all uses the temp engine
    from apps.api.models import (  # noqa: F401 — ensure all models loaded
        Material, Supplier, Project, Quote, AnalysisConfig, BrandTier,
        ExtractionJob, TenderDocument, BidInvitation,
    )

    db_mod.Base.metadata.create_all(bind=engine)

    yield engine, SessionLocal

    engine.dispose()


@pytest.fixture
def db_session(temp_db):
    _, SessionLocal = temp_db
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
