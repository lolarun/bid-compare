"""Integration tests for /api/projects/{id}/quote-rounds (docs/design/42 P0)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _project(client, name="轮次测试项目"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_current_round_is_null_before_any_round_exists(client):
    pid = _project(client)
    r = client.get(f"/api/projects/{pid}/quote-rounds/current", params={"category": "阀门"})
    assert r.status_code == 200
    assert r.json() is None


def test_create_round_then_list_and_get_current(client):
    pid = _project(client)

    r = client.post(f"/api/projects/{pid}/quote-rounds", json={
        "category": "阀门", "name": "招标前摸底", "stage": "pre_tender",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seq"] == 1
    assert body["status"] == "open"
    assert body["stage"] == "pre_tender"
    assert body["is_final_basis"] is False

    cur = client.get(f"/api/projects/{pid}/quote-rounds/current", params={"category": "阀门"})
    assert cur.json()["id"] == body["id"]

    listed = client.get(f"/api/projects/{pid}/quote-rounds", params={"category": "阀门"})
    assert len(listed.json()) == 1


def test_creating_a_second_round_closes_the_first(client):
    pid = _project(client)
    r1 = client.post(f"/api/projects/{pid}/quote-rounds", json={"category": "阀门", "name": "第一轮"})
    r2 = client.post(f"/api/projects/{pid}/quote-rounds", json={"category": "阀门", "name": "第二轮"})
    assert r2.json()["seq"] == 2

    listed = client.get(f"/api/projects/{pid}/quote-rounds", params={"category": "阀门"}).json()
    by_id = {row["id"]: row for row in listed}
    assert by_id[r1.json()["id"]]["status"] == "closed"
    assert by_id[r2.json()["id"]]["status"] == "open"


def test_patch_rename_close_and_set_final_basis(client):
    pid = _project(client)
    r1 = client.post(f"/api/projects/{pid}/quote-rounds", json={"category": "阀门", "name": "第一轮"}).json()

    renamed = client.patch(
        f"/api/projects/{pid}/quote-rounds/{r1['id']}", json={"name": "补充轮"},
    )
    assert renamed.json()["name"] == "补充轮"

    basis = client.patch(
        f"/api/projects/{pid}/quote-rounds/{r1['id']}", json={"is_final_basis": True},
    )
    assert basis.json()["is_final_basis"] is True

    closed = client.patch(
        f"/api/projects/{pid}/quote-rounds/{r1['id']}", json={"status": "closed"},
    )
    assert closed.json()["status"] == "closed"
    # Closing does not clear is_final_basis — the two are independent (docs/design/42 §8 D3).
    assert closed.json()["is_final_basis"] is True


def test_set_final_basis_is_exclusive_via_api(client):
    pid = _project(client)
    r1 = client.post(f"/api/projects/{pid}/quote-rounds", json={"category": "阀门", "name": "第一轮"}).json()
    r2 = client.post(f"/api/projects/{pid}/quote-rounds", json={"category": "阀门", "name": "第二轮"}).json()

    client.patch(f"/api/projects/{pid}/quote-rounds/{r1['id']}", json={"is_final_basis": True})
    r2_flagged = client.patch(
        f"/api/projects/{pid}/quote-rounds/{r2['id']}", json={"is_final_basis": True},
    )
    assert r2_flagged.json()["is_final_basis"] is True

    listed = client.get(f"/api/projects/{pid}/quote-rounds", params={"category": "阀门"}).json()
    basis_flags = {row["id"]: row["is_final_basis"] for row in listed}
    assert basis_flags[r1["id"]] is False
    assert basis_flags[r2["id"]] is True


def test_unknown_project_returns_404(client):
    r = client.get("/api/projects/999999/quote-rounds")
    assert r.status_code == 404


def test_unknown_round_returns_404_on_patch(client):
    pid = _project(client)
    r = client.patch(f"/api/projects/{pid}/quote-rounds/999999", json={"name": "x"})
    assert r.status_code == 404
