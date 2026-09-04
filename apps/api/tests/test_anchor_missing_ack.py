"""docs/design/23 — AnchorMissingAck 服务契约 + 复核矩阵读侧集成。

覆盖：
- set_missing_ack 幂等性（重复 ack 不报错不重复插入；unack 对未确认的格也是
  成功；ack→unack→ack 往返）
- get_missing_ack_set 按 tender_list_session_id 隔离，不跨会话/项目泄漏
- build_anchor_review_matrix 集成：只有真正 acked 的格 missing_acked=True，
  且 cell_status/missing_cells/matrix_distribution 在 ack 前后逐字节一致
  （design/23 §6 的核心安全论证——不是口头保证，这里断言）
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from apps.api.core.errors import ConflictError
from apps.api.models import ExtractionJob, Project, Supplier, TenderListSession
from apps.api.models.anchor_missing_ack import AnchorMissingAck
from apps.api.models.bid_submission import BidSubmission
from apps.api.services.alignment.anchor_missing_ack import (
    get_missing_ack_set,
    set_missing_ack,
)
from apps.api.services.matrix.bid_matrix import build_anchor_review_matrix

CATEGORY = "阀门"


@pytest.fixture
def scope(db_session):
    """1 project, 1 confirmed TenderListSession (2 anchors), 2 submissions.

    没有任何 BidAlignmentGroup/Item —— 两个 submission 在两个锚点上全部是
    missing，正好覆盖 build_anchor_review_matrix 里 group is None 和
    "group 存在但这列没 item" 两条 CELL_MISSING 分支（bid_matrix.py 里对应
    两处分别写 missing_acked 的位置）。
    """
    proj = Project(name="MissingAckProj", status="进行中")
    db_session.add(proj)
    db_session.flush()

    sup_a = Supplier(name="缺报供A", short_name="A", categories=[CATEGORY])
    sup_b = Supplier(name="缺报供B", short_name="B", categories=[CATEGORY])
    db_session.add_all([sup_a, sup_b])
    db_session.flush()

    session = TenderListSession(
        project_id=proj.id, category=CATEGORY, file_name="t.xlsx",
        anchors_total=2, version=1, is_current=True, status="confirmed",
        anchors_json=[
            {"seq": 1, "name": "球阀", "spec": "DN50"},
            {"seq": 2, "name": "闸阀", "spec": "DN80"},
        ],
    )
    db_session.add(session)
    db_session.flush()

    job_a = ExtractionJob(id="job-missing-ack-a", type="quote", status="done")
    job_b = ExtractionJob(id="job-missing-ack-b", type="quote", status="done")
    db_session.add_all([job_a, job_b])
    db_session.flush()

    sub_a = BidSubmission(
        job_id=job_a.id, project_id=proj.id, supplier_id=sup_a.id,
        supplier_raw_name=sup_a.name, status="confirmed", batch_id="BID-a",
    )
    sub_b = BidSubmission(
        job_id=job_b.id, project_id=proj.id, supplier_id=sup_b.id,
        supplier_raw_name=sup_b.name, status="confirmed", batch_id="BID-b",
    )
    db_session.add_all([sub_a, sub_b])
    db_session.commit()

    return {
        "db": db_session, "proj": proj, "sup_a": sup_a, "sup_b": sup_b,
        "session": session, "sub_a": sub_a, "sub_b": sub_b,
    }


# ─── set_missing_ack idempotency ───────────────────────────────────────────────

def test_ack_creates_row(scope):
    s = scope
    row = set_missing_ack(
        s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True,
    )
    assert row is not None
    assert row.anchor_seq == "1"
    assert row.submission_id == s["sub_a"].id
    assert row.tender_list_session_id == s["session"].id

    all_rows = s["db"].scalars(select(AnchorMissingAck)).all()
    assert len(all_rows) == 1


def test_double_ack_is_idempotent_not_duplicate(scope):
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)

    rows = s["db"].scalars(
        select(AnchorMissingAck).where(
            AnchorMissingAck.anchor_seq == "1",
            AnchorMissingAck.submission_id == s["sub_a"].id,
        )
    ).all()
    assert len(rows) == 1


def test_unack_deletes_row(scope):
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    result = set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=False)
    assert result is None

    rows = s["db"].scalars(select(AnchorMissingAck)).all()
    assert len(rows) == 0


def test_unack_on_never_acked_cell_is_a_success_noop(scope):
    """acked=False 对从未确认过的格：不报错、不创建，视为已经是目标状态。"""
    s = scope
    result = set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=False)
    assert result is None
    assert s["db"].scalars(select(AnchorMissingAck)).all() == []


def test_ack_unack_ack_roundtrip(scope):
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=False)
    row = set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    assert row is not None
    assert len(s["db"].scalars(select(AnchorMissingAck)).all()) == 1


def test_ack_without_confirmed_session_raises_conflict(db_session):
    proj = Project(name="NoSessionProj", status="进行中")
    db_session.add(proj)
    db_session.flush()
    with pytest.raises(ConflictError):
        set_missing_ack(db_session, proj.id, "不存在的品类", "1", 999, acked=True)


# ─── get_missing_ack_set scoping ───────────────────────────────────────────────

def test_ack_set_scoped_to_session(scope, db_session):
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "2", s["sub_b"].id, acked=True)

    acked = get_missing_ack_set(db_session, s["session"].id)
    assert acked == {("1", s["sub_a"].id), ("2", s["sub_b"].id)}

    # 不存在的 session_id 不泄漏
    assert get_missing_ack_set(db_session, s["session"].id + 999) == set()


# ─── build_anchor_review_matrix integration ────────────────────────────────────

def test_missing_acked_flows_into_matrix(scope):
    """只有真正 ack 过的 (anchor_seq, submission_id) 显示 missing_acked=True，
    其余 missing 格（含另一个 submission、另一个 anchor）保持 False。"""
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)

    result = build_anchor_review_matrix(
        s["db"], s["proj"].id, CATEGORY,
        submission_ids=[s["sub_a"].id, s["sub_b"].id],
    )
    row1 = next(r for r in result["rows"] if r["anchor_seq"] == "1")
    row2 = next(r for r in result["rows"] if r["anchor_seq"] == "2")

    assert row1["cells"][str(s["sub_a"].id)]["cell_status"] == "missing"
    assert row1["cells"][str(s["sub_a"].id)]["missing_acked"] is True
    assert row1["cells"][str(s["sub_b"].id)]["missing_acked"] is False
    assert row2["cells"][str(s["sub_a"].id)]["missing_acked"] is False
    assert row2["cells"][str(s["sub_b"].id)]["missing_acked"] is False


def test_ack_does_not_change_cell_status_or_counts(scope):
    """design/23 §6 的安全论证：acked 前后 cell_status/missing_cells/
    matrix_distribution 必须逐字节一致——ack 只是叠加的展示标记。"""
    s = scope
    before = build_anchor_review_matrix(
        s["db"], s["proj"].id, CATEGORY,
        submission_ids=[s["sub_a"].id, s["sub_b"].id],
    )

    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)

    after = build_anchor_review_matrix(
        s["db"], s["proj"].id, CATEGORY,
        submission_ids=[s["sub_a"].id, s["sub_b"].id],
    )

    assert before["missing_cells"] == after["missing_cells"]
    assert before["pending_cells"] == after["pending_cells"]
    assert before["quoted_ge_2_count"] == after["quoted_ge_2_count"]
    assert before["quoted_full_count"] == after["quoted_full_count"]
    assert before["matrix_distribution"] == after["matrix_distribution"]
    for b_row, a_row in zip(before["rows"], after["rows"]):
        assert b_row["row_status"] == a_row["row_status"]
        assert b_row["quoted_count"] == a_row["quoted_count"]
        assert b_row["covered_count"] == a_row["covered_count"]
        for col in (str(s["sub_a"].id), str(s["sub_b"].id)):
            assert b_row["cells"][col]["cell_status"] == a_row["cells"][col]["cell_status"]


def test_unack_reverts_flag(scope):
    s = scope
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=True)
    set_missing_ack(s["db"], s["proj"].id, CATEGORY, "1", s["sub_a"].id, acked=False)

    result = build_anchor_review_matrix(
        s["db"], s["proj"].id, CATEGORY,
        submission_ids=[s["sub_a"].id, s["sub_b"].id],
    )
    row1 = next(r for r in result["rows"] if r["anchor_seq"] == "1")
    assert row1["cells"][str(s["sub_a"].id)]["missing_acked"] is False


# ─── POST /anchor-review/missing-ack (route + audit event) ────────────────────

@pytest.fixture
def api_client(scope):
    from fastapi.testclient import TestClient

    from apps.api.core.database import get_db
    from apps.api.main import app
    from apps.api.routes.auth import get_current_user

    def override_db():
        yield scope["db"]

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "role": "管理员"}
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_route_ack_then_unack(scope, api_client):
    s = scope
    body = {
        "project_id": s["proj"].id, "category": CATEGORY,
        "anchor_seq": "1", "submission_id": s["sub_a"].id, "acked": True,
    }
    r = api_client.post("/api/analysis/anchor-review/missing-ack", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data == {"ok": True, "anchor_seq": "1", "submission_id": s["sub_a"].id, "acked": True}
    assert len(s["db"].scalars(select(AnchorMissingAck)).all()) == 1

    # 幂等重复 ack
    r2 = api_client.post("/api/analysis/anchor-review/missing-ack", json=body)
    assert r2.status_code == 200, r2.text
    assert len(s["db"].scalars(select(AnchorMissingAck)).all()) == 1

    # unack
    body["acked"] = False
    r3 = api_client.post("/api/analysis/anchor-review/missing-ack", json=body)
    assert r3.status_code == 200, r3.text
    assert r3.json()["acked"] is False
    assert s["db"].scalars(select(AnchorMissingAck)).all() == []


def test_route_writes_audit_event(scope, api_client):
    from apps.api.models.operation_log import OperationLog
    from apps.api.services.audit import EVENT_ANCHOR_MISSING_ACK

    s = scope
    api_client.post("/api/analysis/anchor-review/missing-ack", json={
        "project_id": s["proj"].id, "category": CATEGORY,
        "anchor_seq": "1", "submission_id": s["sub_a"].id, "acked": True,
    })
    logs = s["db"].scalars(
        select(OperationLog).where(OperationLog.action == EVENT_ANCHOR_MISSING_ACK)
    ).all()
    assert len(logs) == 1


def test_route_without_confirmed_session_is_409(scope, api_client):
    r = api_client.post("/api/analysis/anchor-review/missing-ack", json={
        "project_id": scope["proj"].id, "category": "不存在的品类",
        "anchor_seq": "1", "submission_id": 999, "acked": True,
    })
    assert r.status_code == 409, r.text
