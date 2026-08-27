"""docs/design/44 §3.2 — GET /api/projects/overview.

The比价入口列表 read-only aggregate: one row per project, one entry per
category that has ever had a QuoteRound. Locks the two things a reader of
the endpoint needs to trust: a project with no rounds still appears (empty
categories, not dropped), and multi-category projects get one summary per
category, not one merged/overwritten summary.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def test_project_with_no_rounds_appears_with_empty_categories(client):
    resp = client.post("/api/projects", json={"name": "尚无轮次的项目", "code": "OV1"})
    assert resp.status_code == 201

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV1")
    assert row["categories"] == []


def test_project_with_one_round_shows_current_round(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "单轮项目", "code": "OV2"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV2")
    assert len(row["categories"]) == 1
    cat = row["categories"][0]
    assert cat["category"] == "阀门"
    assert cat["current_round"]["seq"] == 1
    assert cat["current_round"]["status"] == "open"
    assert cat["round_count"] == 1
    assert cat["final_basis_round"] is None


def test_multi_category_project_gets_one_summary_per_category(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "多品类项目", "code": "OV3"}).json()
    svc.create_round(db_session, proj["id"], "阀门", name="阀门第一轮")
    svc.create_round(db_session, proj["id"], "电缆", name="电缆第一轮")
    db_session.commit()

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV3")
    cats = {c["category"] for c in row["categories"]}
    assert cats == {"阀门", "电缆"}


def test_current_round_is_the_latest_seq_not_the_first(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "两轮项目", "code": "OV4"}).json()
    svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    svc.create_round(db_session, proj["id"], "阀门", name="第二轮")
    db_session.commit()

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV4")
    cat = row["categories"][0]
    assert cat["current_round"]["seq"] == 2
    assert cat["round_count"] == 2


def test_final_basis_round_surfaced_when_set(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "定标项目", "code": "OV5"}).json()
    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    svc.set_final_basis(db_session, r1.id, True)

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV5")
    cat = row["categories"][0]
    assert cat["final_basis_round"]["id"] == r1.id
