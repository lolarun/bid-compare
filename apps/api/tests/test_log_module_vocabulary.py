"""OperationLog.module vocabulary — canonical slugs + the filter options API.

The operation-log page's module filter used to carry a hand-written option
list ("招标比价分析", "邀标建议", …) that matched no stored row, so picking a
module silently returned zero logs. These tests pin the two halves of the fix:
every write path uses a registered slug, and GET /api/logs/modules serves the
filter vocabulary so the UI cannot drift again.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api.core import database as db_mod
from apps.api.core.enums import (
    LOG_MODULE_BID_COMPARE,
    LOG_MODULE_LABELS,
    LOG_MODULE_SYSTEM,
    LOG_MODULE_USER,
    ROLE_ADMIN,
)
from apps.api.models.operation_log import OperationLog
from apps.api.models.user import User


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'logs.db'}", connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", SessionLocal)
    import apps.api.models  # noqa: F401 — register all models

    db_mod.Base.metadata.create_all(bind=engine)
    return engine, SessionLocal


@pytest.fixture
def client(temp_db):
    from apps.api.core.database import get_db
    from apps.api.main import app

    _, SessionLocal = temp_db

    def _get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _admin_header(client, SessionLocal) -> dict:
    db = SessionLocal()
    user = User(username="admin_logs", nickname="admin", role=ROLE_ADMIN, status="启用")
    user.set_password("pw12345678")
    db.add(user)
    db.commit()
    db.close()
    resp = client.post(
        "/api/auth/login", json={"username": "admin_logs", "password": "pw12345678"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── 1. Write paths use registered slugs ──────────────────────────────────────

def test_login_writes_registered_system_slug(client, temp_db):
    _, SessionLocal = temp_db
    _admin_header(client, SessionLocal)

    db = SessionLocal()
    modules = {row.module for row in db.query(OperationLog).all()}
    db.close()
    assert modules == {LOG_MODULE_SYSTEM}
    assert modules <= set(LOG_MODULE_LABELS)


def test_user_crud_and_domain_events_use_registered_slugs(client, temp_db):
    from apps.api.services.audit import write_domain_event

    _, SessionLocal = temp_db
    headers = _admin_header(client, SessionLocal)

    resp = client.post(
        "/api/users",
        json={
            "username": "buyer1",
            "password": "pw12345678",
            "nickname": "buyer",
            "role": "比价员",
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text

    db = SessionLocal()
    write_domain_event(
        db, user="admin_logs", event_type="bql_confirm", identity={"project_id": 1}
    )
    db.commit()
    modules = {row.module for row in db.query(OperationLog).all()}
    db.close()

    assert modules == {LOG_MODULE_SYSTEM, LOG_MODULE_USER, LOG_MODULE_BID_COMPARE}
    # Every module a write path produces must be filterable from the UI.
    assert modules <= set(LOG_MODULE_LABELS)


# ── 2. The filter vocabulary endpoint ────────────────────────────────────────

def test_modules_endpoint_serves_registry_with_labels(client, temp_db):
    _, SessionLocal = temp_db
    headers = _admin_header(client, SessionLocal)

    resp = client.get("/api/logs/modules", headers=headers)
    assert resp.status_code == 200, resp.text
    options = resp.json()
    assert [o["value"] for o in options] == list(LOG_MODULE_LABELS)
    assert {o["value"]: o["label"] for o in options} == LOG_MODULE_LABELS


def test_modules_endpoint_surfaces_unregistered_stored_values(client, temp_db):
    """An unregistered writer must show up, not become silently unfilterable."""
    _, SessionLocal = temp_db
    headers = _admin_header(client, SessionLocal)

    db = SessionLocal()
    db.add(OperationLog(user="x", module="rogue-module", action="poke"))
    db.commit()
    db.close()

    options = client.get("/api/logs/modules", headers=headers).json()
    rogue = [o for o in options if o["value"] == "rogue-module"]
    assert rogue == [{"value": "rogue-module", "label": "rogue-module"}]


def test_every_served_option_matches_at_least_its_own_rows(client, temp_db):
    """The original defect: a served option that the list filter can never match."""
    _, SessionLocal = temp_db
    headers = _admin_header(client, SessionLocal)

    db = SessionLocal()
    for slug in LOG_MODULE_LABELS:
        db.add(OperationLog(user="x", module=slug, action="probe"))
    db.commit()
    db.close()

    for option in client.get("/api/logs/modules", headers=headers).json():
        body = client.get(
            "/api/logs", params={"module": option["value"]}, headers=headers
        ).json()
        assert body["total"] > 0, f"option {option['value']!r} matches no rows"


# ── 3. Migration 0013 normalises legacy Chinese values ───────────────────────

def _load_migration_0013():
    """Load the revision by path — the filename is not a Python identifier."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(db_mod.__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0013_operation_log_module_slug.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_0013", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0013_rewrites_legacy_values_in_place(tmp_path):
    mig = _load_migration_0013()

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE operation_logs (id INTEGER PRIMARY KEY, module TEXT)"
            )
        )
        for module in ["系统"] * 3 + ["用户管理"] * 2 + ["bid-compare"]:
            conn.execute(
                text("INSERT INTO operation_logs (module) VALUES (:m)"), {"m": module}
            )

    with engine.begin() as conn:
        mig._rename(conn, mig._LEGACY_TO_SLUG)
        counts = dict(
            conn.execute(
                text("SELECT module, COUNT(*) FROM operation_logs GROUP BY module")
            ).all()
        )
    assert counts == {"system": 3, "user-management": 2, "bid-compare": 1}

    # Idempotent: a second run is a no-op.
    with engine.begin() as conn:
        mig._rename(conn, mig._LEGACY_TO_SLUG)
        again = dict(
            conn.execute(
                text("SELECT module, COUNT(*) FROM operation_logs GROUP BY module")
            ).all()
        )
    assert again == counts
    engine.dispose()
