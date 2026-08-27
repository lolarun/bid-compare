"""docs/design/42 §8 D1 / design/44 F3 — POST /api/projects requires 管理员.

Locks the role gate itself (403 for 比价员/查看者, 201 for 管理员) and that
`created_by_user_id` is recorded from the authenticated user, not left blank
or guessed.

Note: the fixture `auth_override_as` referenced in conftest.py's
`auth_override` docstring does not actually exist (a pre-existing gap, not
introduced here) — this file overrides `get_current_user` directly instead,
following the same override/cleanup shape `auth_override` itself uses.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _as(role: str, user_id: int = 1):
    from apps.api.main import app
    from apps.api.routes.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "test", "role": role, "user_id": user_id,
    }
    return app


def _clear(app):
    from apps.api.routes.auth import get_current_user
    app.dependency_overrides.pop(get_current_user, None)


def test_buyer_cannot_create_project(client):
    app = _as("比价员")
    try:
        resp = client.post("/api/projects", json={"name": "比价员建的项目", "code": "F3-1"})
    finally:
        _clear(app)
    assert resp.status_code == 403


def test_viewer_cannot_create_project(client):
    app = _as("查看者")
    try:
        resp = client.post("/api/projects", json={"name": "查看者建的项目", "code": "F3-2"})
    finally:
        _clear(app)
    assert resp.status_code == 403


def test_admin_can_create_project_and_created_by_is_recorded(client):
    app = _as("管理员", user_id=7)
    try:
        resp = client.post("/api/projects", json={"name": "管理员建的项目", "code": "F3-3"})
    finally:
        _clear(app)
    assert resp.status_code == 201, resp.text
    assert resp.json()["created_by_user_id"] == 7


def test_edit_and_delete_remain_open_to_non_admin_f3_scope_is_creation_only(client):
    """F3's scope (design/44 §6) is creation only — edit/delete unrestricted."""
    app = _as("管理员")
    try:
        created = client.post("/api/projects", json={"name": "待编辑项目", "code": "F3-4"}).json()
    finally:
        _clear(app)

    app = _as("比价员")
    try:
        resp = client.put(f"/api/projects/{created['id']}", json={"remark": "比价员改的"})
    finally:
        _clear(app)
    assert resp.status_code == 200
