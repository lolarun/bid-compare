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


@pytest.fixture
def auth_override():
    """Override get_current_user on the shared `app` singleton, then clean up.

    评审 G1：main.py 给除 auth 路由外的一切路由挂了全局鉴权依赖
    （`dependencies=[Depends(get_current_user)]`）。多个测试文件各自手写
    `app.dependency_overrides[get_current_user] = ...` 来绕过它，但 `app` 是
    跨测试共享的单例，覆盖若不清理就会一直生效——某个测试文件按字母序先跑
    并留下覆盖，字母序更靠后、自己完全没设置鉴权的测试文件也会"以管理员身份"
    悄悄通过（test_intake_routes.py 就是这样：单独跑该文件 7/7 全部 401，
    混在全量套件里却是绿的）。且鉴权本身因此从未被独立测试过。

    用这个 fixture 而不是手写覆盖：无论测试正常结束还是抛异常都会在 yield 后
    弹出覆盖，不留全局状态给下一个测试文件捡漏。

    用法：`def client(self, auth_override): ...` 或直接依赖它触发副作用；
    需要自定义身份时改用 `auth_override_as(role=..., sub=...)`。
    """
    from apps.api.main import app
    from apps.api.routes.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "role": "管理员"}
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)
