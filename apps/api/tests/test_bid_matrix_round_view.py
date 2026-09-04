"""docs/design/44 §4.4 — viewing a specific (usually closed) round's matrix.

`POST /api/analysis/bid-matrix` with `round_id` set is a separate branch from
the "current state" path: it reads `QuoteRound.used_submission_ids` (frozen
at match time) instead of `TenderListSession.used_submission_ids` (shared,
overwritten by whichever round matched most recently), and skips the
active-state hard gates entirely — a closed round's scope was frozen when it
closed, not something to re-validate against "what's confirmed right now".
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api.models.extraction_job import ExtractionJob
from apps.api.routes.quotes import BatchConfirmRequest
from apps.api.services.alignment.anchor_match import import_and_match
from apps.api.services.submission.quote_confirmation_service import confirm_batch
from apps.api.services.tender import quote_round_service as svc
from apps.api.services.tender import tender_session_service as tls_svc
from apps.api.services.tender.tender_list import rebuild_anchors

ANCHORS_JSON = [
    {"seq": "1", "name": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10},
    {"seq": "2", "name": "闸阀DN50", "spec": "Z45X-16Q", "unit": "个", "qty": 5},
]


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _confirm(db, project_id: int, category: str, round_id: int, supplier: str,
             price100: float) -> int:
    j = ExtractionJob(
        id=uuid.uuid4().hex, type="quote", status="done", filename=f"{supplier}.png",
        result={"items": [
            {"material": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10,
             "unit_price": price100, "total_price": price100 * 10, "category": category},
            {"material": "闸阀DN50", "spec": "Z45X-16Q", "unit": "个", "qty": 5,
             "unit_price": 80.0, "total_price": 400.0, "category": category},
        ]},
    )
    db.add(j)
    db.flush()
    db.commit()
    out = confirm_batch(db, BatchConfirmRequest(
        job_id=j.id, supplier_name=supplier, project_id=project_id,
        category=category, round_id=round_id,
    ))
    return out["submission_id"]


def _run_round(db, project_id, category, round_id, session_id, sub_ids):
    session = tls_svc.get_current_session_any_status(db, category, project_id=project_id)
    anchors = rebuild_anchors(session)
    import_and_match(
        db, None, project_id, category, submission_ids=sub_ids, anchors=anchors,
        tender_list_session_id=session_id, round_id=round_id,
    )
    svc.record_round_scope(db, round_id, sorted(sub_ids), None, tender_list_session_id=session_id)


def test_round_id_returns_that_rounds_own_frozen_scope(client, db_session):
    proj = client.post("/api/projects", json={"name": "历史矩阵项目", "code": "RV1"}).json()
    session = tls_svc.save_session(db_session, proj["id"], "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm(db_session, proj["id"], "阀门", r1.id, "供应商A", price100=100.0)
    _run_round(db_session, proj["id"], "阀门", r1.id, session.id, [sub1])
    svc.close_round(db_session, r1.id)

    r2 = svc.create_round(db_session, proj["id"], "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm(db_session, proj["id"], "阀门", r2.id, "供应商A", price100=90.0)
    _run_round(db_session, proj["id"], "阀门", r2.id, session.id, [sub2])

    resp = client.post("/api/analysis/bid-matrix", json={
        "project_id": proj["id"], "supplier_ids": [], "category": "阀门", "round_id": r1.id,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["round_id"] == r1.id
    assert data["round_readonly"] is True
    # Round 1's own price (100.0), not round 2's (90.0) — proves it's reading
    # r1's frozen used_submission_ids, not "whatever is confirmed right now".
    cell = data["rows"][0]["suppliers"][0]
    assert cell["price"] == pytest.approx(100.0)


def test_round_id_view_bypasses_the_active_state_hard_gate(client, db_session):
    """A closed round with no live TenderListSession.used_submission_ids
    consistency (because round 2 has since overwritten it) must still be
    viewable — that's the entire point of design/42 §3.1."""
    proj = client.post("/api/projects", json={"name": "闸门旁路项目", "code": "RV2"}).json()
    session = tls_svc.save_session(db_session, proj["id"], "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm(db_session, proj["id"], "阀门", r1.id, "供应商A", price100=100.0)
    _run_round(db_session, proj["id"], "阀门", r1.id, session.id, [sub1])
    svc.close_round(db_session, r1.id)

    r2 = svc.create_round(db_session, proj["id"], "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm(db_session, proj["id"], "阀门", r2.id, "供应商B", price100=95.0)
    _run_round(db_session, proj["id"], "阀门", r2.id, session.id, [sub2])
    # TenderListSession.used_submission_ids now reflects round 2, not round 1
    # (record_submission_scope's legacy write) — the "current state" path
    # would 409 for round 1's own submission_ids not matching. round_id
    # sidesteps it entirely.

    resp = client.post("/api/analysis/bid-matrix", json={
        "project_id": proj["id"], "supplier_ids": [], "category": "阀门", "round_id": r1.id,
    })
    assert resp.status_code == 200, resp.text


def test_round_without_match_ever_run_returns_409_not_empty_matrix(client, db_session):
    proj = client.post("/api/projects", json={"name": "无匹配轮次项目", "code": "RV3"}).json()
    tls_svc.save_session(db_session, proj["id"], "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()
    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()

    resp = client.post("/api/analysis/bid-matrix", json={
        "project_id": proj["id"], "supplier_ids": [], "category": "阀门", "round_id": r1.id,
    })
    assert resp.status_code == 409


def test_unknown_round_id_returns_404(client, db_session):
    proj = client.post("/api/projects", json={"name": "假轮次项目", "code": "RV4"}).json()

    resp = client.post("/api/analysis/bid-matrix", json={
        "project_id": proj["id"], "supplier_ids": [], "category": "阀门", "round_id": 999999,
    })
    assert resp.status_code == 404


def test_second_round_matrix_without_round_id_still_finds_its_own_groups(client, db_session):
    """**回归**：不传 `round_id` 时，第 2 轮的报价必须能出价，不能全是"未报价"。

    2026-09-04 线上复现（project 58）：第 2 轮进比价分析，89 行全部显示"未报价"，
    而库里躺着 89 条 `action='align'` 的对齐项。

    根因：路由在"当前状态"这条分支从不传 `round_id`，于是 group 查询把**各轮的
    group 全部载入**，而 `seq_to_group` 按 `anchor_seq` 先到先得建索引——第 1 轮
    的 group 先占满全部 anchor_seq，第 2 轮的同名 group 被静默丢弃，它的对齐项
    因此一条都查不到。

    这个 bug 能活到今天，是因为既有的多轮矩阵测试**全都显式传 `round_id`**，
    覆盖的是"看历史轮快照"那条分支；前端实际走的这条从没被多轮场景测过。
    """
    proj = client.post("/api/projects", json={"name": "多轮当前态", "code": "RV9"}).json()
    session = tls_svc.save_session(db_session, proj["id"], "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm(db_session, proj["id"], "阀门", r1.id, "供应商A", price100=100.0)
    _run_round(db_session, proj["id"], "阀门", r1.id, session.id, [sub1])
    svc.close_round(db_session, r1.id)

    r2 = svc.create_round(db_session, proj["id"], "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm(db_session, proj["id"], "阀门", r2.id, "供应商A", price100=90.0)
    _run_round(db_session, proj["id"], "阀门", r2.id, session.id, [sub2])
    # `TenderListSession.used_submission_ids` 由最后一次 match 覆盖（这正是
    # round_id 分支存在的理由）。这里显式写成第 2 轮的范围，复现线上状态，
    # 否则会先撞上 `alignment_not_run` 那道**另一道**闸门，测不到本 bug。
    session.used_submission_ids = [sub2]
    db_session.add(session)
    db_session.commit()

    # 前端就是这么调的：给 submission_ids，**不给 round_id**
    resp = client.post("/api/analysis/bid-matrix", json={
        "project_id": proj["id"], "supplier_ids": [], "category": "阀门",
        "submission_ids": [sub2],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    cell = data["rows"][0]["suppliers"][0]
    assert cell["cell_status"] != "missing", (
        f"第 2 轮的报价被判成未报价了：{cell}"
    )
    # 拿到的必须是第 2 轮自己的价（90），不是第 1 轮的（100）
    assert cell["price"] == pytest.approx(90.0)

    quoted = sum(
        1 for row in data["rows"]
        for c in row["suppliers"]
        if c["cell_status"] in ("quoted", "aggregated")
    )
    assert quoted > 0, "整张矩阵一个报价格子都没有"
