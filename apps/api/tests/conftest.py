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


# ── 遗留夹具（测试单根合并，2026-08-28）──────────────────────────────────────
# 以下从已废弃的根 `tests/conftest.py` 移入，专供从那里搬来的老测试文件用
# （test_analysis.py / test_api.py / test_models.py / test_new_api.py）。
#
# 命名刻意避开 `client`/`db_session`——本文件上面的 `db_session`/`auth_override`
# 已是全仓其它测试文件在用的现行约定，若在同一个 conftest.py 里用同名重新定义
# 一遍会静默覆盖它们，波及这个目录下所有其它测试。`legacy_client` 内部直接复
# 用现行的 `auth_override` + `db_session`，不是另一套鉴权实现。
# `sample_material`/`sample_supplier`/`sample_project`/`sample_quotes` 同样建
# 在现行 `db_session` 之上，而不是重新定义一个 db_session。


@pytest.fixture
def legacy_client(auth_override, db_session):
    """遗留 TestClient 夹具：等价于旧 `tests/conftest.py::client`，但鉴权走
    现行的 `auth_override`（会在测试结束后清理覆盖），不是各自手写、忘记清理
    的旧写法。

    **预置 `AnalysisConfig` 两行**（`scoring_weights`/`thresholds`）——照搬旧
    `tests/conftest.py::db_session` 的做法。这不是多余的：`GET` 系读取路径对
    缺行有默认值兜底（`services/history/scoring.py`/`comparison.py`），但
    `PUT /api/config/{key}`（`routes/config.py::update_config`）要求该行必须
    已存在，否则 404——单纯迁移到现行 `db_session`（不预置）会让
    `test_update_config` 从"更新一行"变成"更新一个不存在的东西"，2026-08-28
    测试单根合并时踩过一次，这里补回来。
    """
    from apps.api.core.database import get_db
    from apps.api.core.config import DEFAULT_SCORING_WEIGHTS, DEFAULT_THRESHOLDS
    from apps.api.main import app
    from apps.api.models import AnalysisConfig
    from fastapi.testclient import TestClient

    db_session.add(AnalysisConfig(key="scoring_weights", value=DEFAULT_SCORING_WEIGHTS, description="test"))
    db_session.add(AnalysisConfig(key="thresholds", value=DEFAULT_THRESHOLDS, description="test"))
    db_session.commit()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def sample_material(db_session):
    """插入一条示例材料。"""
    from apps.api.models import Material

    mat = Material(
        material_code="EL-BRG-00001",
        standard_name="托盘式热浸镀锌桥架",
        profession="电气",
        category="桥架",
        sub_category="托盘式桥架",
        spec="300×150",
        material_type="热浸镀锌",
        unit="m",
        brand="某品牌",
        ref_price_median=50.0,
        ref_price_avg=52.0,
        deviation_threshold=0.10,
    )
    db_session.add(mat)
    db_session.commit()
    db_session.refresh(mat)
    return mat


@pytest.fixture
def sample_supplier(db_session):
    """插入一条示例供应商。"""
    from apps.api.models import Supplier

    sup = Supplier(
        name="测试供应商A",
        short_name="供A",
        categories=["桥架", "母线槽"],
        win_count=3,
        cooperation_score=75.0,
    )
    db_session.add(sup)
    db_session.commit()
    db_session.refresh(sup)
    return sup


@pytest.fixture
def sample_project(db_session):
    """插入一条示例项目。"""
    from apps.api.models import Project

    proj = Project(name="测试项目一期", code="P2025-001", status="进行中")
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj


@pytest.fixture
def sample_quotes(db_session, sample_material, sample_supplier, sample_project):
    """为示例材料插入若干报价。"""
    from apps.api.models import Quote

    prices = [45.0, 48.0, 50.0, 52.0, 55.0, 53.0, 47.0, 51.0]
    quotes = []
    for p in prices:
        q = Quote(
            material_id=sample_material.id,
            supplier_id=sample_supplier.id,
            project_id=sample_project.id,
            unit_price=p,
            quantity=100.0,
            brand=sample_supplier.name,
        )
        db_session.add(q)
        quotes.append(q)
    db_session.commit()
    return quotes
