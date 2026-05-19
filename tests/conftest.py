"""Shared pytest fixtures for backend tests."""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from apps.api.core.database import Base, get_db
from apps.api.models import (
    Material, Supplier, Project, Quote, AnalysisConfig,
    DEFAULT_SCORING_WEIGHTS, DEFAULT_THRESHOLDS,
)
from apps.api.main import app

from fastapi.testclient import TestClient


@pytest.fixture(scope="function")
def db_session(tmp_path):
    """Create a fresh in-memory SQLite database for each test."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestSession()

    # Seed default configs
    session.add(AnalysisConfig(key="scoring_weights", value=DEFAULT_SCORING_WEIGHTS, description="test"))
    session.add(AnalysisConfig(key="thresholds", value=DEFAULT_THRESHOLDS, description="test"))
    session.commit()

    yield session
    session.close()


@pytest.fixture(scope="function")
def client(db_session):
    """FastAPI test client with overridden DB dependency."""
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def sample_material(db_session):
    """Insert a sample material and return it."""
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
    """Insert a sample supplier and return it."""
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
    """Insert a sample project and return it."""
    proj = Project(name="测试项目一期", code="P2025-001", status="进行中")
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj


@pytest.fixture
def sample_quotes(db_session, sample_material, sample_supplier, sample_project):
    """Insert several quotes for the sample material."""
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
