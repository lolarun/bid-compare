"""DELETE /quotes/submissions[/{id}] 软删除（标记 superseded）回归测试。

覆盖：
- 逐个移除：单条 submission → superseded，幂等再删返回 already=True，404。
- 一键移除：项目下全部 active submission → superseded，已 superseded/rejected 不计入。
- 软删除后不物理删除：行仍在库，仅 status 改变（§12）。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.core.database import Base, get_db
from apps.api.main import app
from apps.api.models import Project
from apps.api.models.bid_submission import BidSubmission
from apps.api.models.extraction_job import ExtractionJob
from apps.api.routes.auth import get_current_user


@pytest.fixture(scope="module")
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import apps.api.models  # noqa: F401  (register all models)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(scope="module")
def client(db_session):
    app.dependency_overrides[get_db] = lambda: (yield db_session)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "t", "role": "管理员"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _mk_sub(db, project_id, name, status="pending"):
    s = BidSubmission(
        job_id=f"job-{name}",
        supplier_raw_name=name,
        project_id=project_id,
        batch_id=f"BID-{name}",
        status=status,
    )
    # 匹配的 quote job（已入库 → lifecycle=confirmed），供 supersede 翻成 removed
    db.add(ExtractionJob(
        id=f"job-{name}", type="quote", status="done", lifecycle="confirmed",
        filename=f"{name}.pdf", context={"project_id": project_id},
    ))
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def test_supersede_single(client, db_session):
    proj = Project(name="P-single", code="PS-1")
    db_session.add(proj)
    db_session.commit()
    sub = _mk_sub(db_session, proj.id, "A")

    r = client.delete(f"/api/quotes/submissions/{sub.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "superseded" and body["already"] is False

    db_session.refresh(sub)
    assert sub.status == "superseded"
    # 软删除：记录仍在库，未物理删除
    assert db_session.get(BidSubmission, sub.id) is not None


def test_supersede_single_idempotent_and_404(client, db_session):
    proj = Project(name="P-idem", code="PI-1")
    db_session.add(proj)
    db_session.commit()
    sub = _mk_sub(db_session, proj.id, "B")

    assert client.delete(f"/api/quotes/submissions/{sub.id}").status_code == 200
    second = client.delete(f"/api/quotes/submissions/{sub.id}")
    assert second.status_code == 200 and second.json()["already"] is True

    assert client.delete("/api/quotes/submissions/999999").status_code == 404


def test_supersede_all_in_project(client, db_session):
    proj = Project(name="P-all", code="PA-1")
    db_session.add(proj)
    db_session.commit()
    a = _mk_sub(db_session, proj.id, "all-A", status="pending")
    b = _mk_sub(db_session, proj.id, "all-B", status="confirmed")
    already = _mk_sub(db_session, proj.id, "all-C", status="rejected")
    # 另一个项目的 submission 不应被波及
    other = Project(name="P-other", code="PO-1")
    db_session.add(other)
    db_session.commit()
    untouched = _mk_sub(db_session, other.id, "other-X", status="pending")

    r = client.delete("/api/quotes/submissions", params={"project_id": proj.id})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert set(body["superseded_ids"]) == {a.id, b.id}

    for s in (a, b):
        db_session.refresh(s)
        assert s.status == "superseded"
    db_session.refresh(already)
    assert already.status == "rejected"  # 已 rejected 不重复处理
    db_session.refresh(untouched)
    assert untouched.status == "pending"  # 跨项目隔离

    # 对应 job 生命周期同步 removed；跨项目 job 不受影响
    for s in (a, b):
        assert db_session.get(ExtractionJob, s.job_id).lifecycle == "removed"
    assert db_session.get(ExtractionJob, untouched.job_id).lifecycle == "confirmed"


def test_compare_state_excludes_removed_job_keeps_inflight(client, db_session):
    """核心回归：submission 被移除后，其 job 不得作为在途重新出现；
    真正在途（lifecycle=active，无 submission）的 job 仍显示。"""
    proj = Project(name="P-cs", code="PCS-1")
    db_session.add(proj)
    db_session.commit()
    pid = proj.id

    # 1) 已入库后被移除的文件
    removed_sub = _mk_sub(db_session, pid, "cs-removed", status="pending")
    # 2) 真正在途：已识别待确认，无 submission
    db_session.add(ExtractionJob(
        id="job-cs-inflight", type="quote", status="done", lifecycle="active",
        filename="在途.pdf", context={"project_id": pid},
    ))
    db_session.commit()

    def _state():
        r = client.get("/api/analysis/compare-state", params={"project_id": pid})
        assert r.status_code == 200
        return r.json()

    # 移除前：submission 在列表，在途 job 在 inflight
    before = _state()
    assert removed_sub.id in [s["submission_id"] for s in before["submissions"]]
    assert "job-cs-inflight" in [j["job_id"] for j in before["inflight_jobs"]]

    # 移除已入库文件
    assert client.delete(f"/api/quotes/submissions/{removed_sub.id}").status_code == 200

    after = _state()
    # 被移除的：既不在 submissions，也不作为在途重现
    assert removed_sub.id not in [s["submission_id"] for s in after["submissions"]]
    assert removed_sub.job_id not in [j["job_id"] for j in after["inflight_jobs"]]
    # 真正在途的：仍然显示
    assert "job-cs-inflight" in [j["job_id"] for j in after["inflight_jobs"]]


def test_remove_failed_inflight_job(client, db_session):
    """失败/在途 job（无 submission）可经 DELETE /quotes/jobs/{id} 移除，
    标记 removed 后 compare-state 不再返回；幂等；404。"""
    proj = Project(name="P-job", code="PJ-1")
    db_session.add(proj)
    db_session.commit()
    pid = proj.id
    db_session.add(ExtractionJob(
        id="job-failed-1", type="quote", status="failed", lifecycle="active",
        filename="失败.pdf", context={"project_id": pid},
    ))
    db_session.commit()

    # 移除前在途可见
    r0 = client.get("/api/analysis/compare-state", params={"project_id": pid})
    assert "job-failed-1" in [j["job_id"] for j in r0.json()["inflight_jobs"]]

    r = client.delete("/api/quotes/jobs/job-failed-1")
    assert r.status_code == 200 and r.json()["already"] is False
    assert db_session.get(ExtractionJob, "job-failed-1").lifecycle == "removed"

    # 移除后不再作为在途返回
    r1 = client.get("/api/analysis/compare-state", params={"project_id": pid})
    assert "job-failed-1" not in [j["job_id"] for j in r1.json()["inflight_jobs"]]

    # 幂等 + 404
    assert client.delete("/api/quotes/jobs/job-failed-1").json()["already"] is True
    assert client.delete("/api/quotes/jobs/nope").status_code == 404
