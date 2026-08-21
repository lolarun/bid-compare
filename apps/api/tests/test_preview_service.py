"""design/31 cut 2b：预览比价编排器的集成测试。

两件事要证明，缺一不可：
1. 它真的**算出了**比价结果（跑通了官方链路，不是返回一个空壳）；
2. 它真的**什么也没写进库**（这是 A 方案的全部安全性所在）。

只证 1 就是把一个会污染数据库的功能当成功了；只证 2 就是把一个什么也不做的
函数当安全了。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import func, select

from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.routes.quotes import BatchConfirmRequest
from apps.api.services.matrix.preview_service import PreviewNotReady, build_preview_matrix

# 复用既有集成夹具：同一套 mock provider + TestClient，别另起一套。
from apps.api.tests.test_compare_integration import compare_client  # noqa: F401


def _png(color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


def _upload_quote(client, name: str, color) -> str:
    r = client.post(
        "/api/intake/upload",
        data={"type": "quote", "category": "阀门"},
        files={"file": (name, _png(color), "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _setup_unconfirmed(client) -> dict:
    """建一个"采购清单已确认、但报价一份都没入库"的项目——正是预览要服务的状态。"""
    r = client.post("/api/projects", json={"name": "预览比价项目", "code": "PV-E2E"})
    assert r.status_code in (200, 201), r.text
    project_id = r.json()["id"]

    job_a = _upload_quote(client, "A.png", (255, 255, 255))
    job_b = _upload_quote(client, "B.png", (250, 250, 250))

    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": project_id, "category": "阀门",
        "file_name": "test.xlsx",
        "anchors_json": [
            {"seq": "1", "name": "DN100 闸阀", "spec": "Z45X-16Q", "unit": "个", "qty": 10, "category": "阀门"},
            {"seq": "2", "name": "DN50 闸阀", "spec": "Z45X-16Q", "unit": "个", "qty": 20, "category": "阀门"},
        ],
        "anchors_total": 2, "source_type": "excel",
    })
    assert r.status_code == 200, r.text

    return {
        "project_id": project_id,
        "confirmations": [
            BatchConfirmRequest(job_id=job_a, supplier_name="供应商A",
                                project_id=project_id, category="阀门"),
            BatchConfirmRequest(job_id=job_b, supplier_name="供应商B",
                                project_id=project_id, category="阀门"),
        ],
    }


def _counts(SessionLocal) -> tuple[int, int]:
    with SessionLocal() as s:
        return (
            s.scalar(select(func.count()).select_from(BidSubmission)) or 0,
            s.scalar(select(func.count()).select_from(BidQuoteLine)) or 0,
        )


def test_preview_produces_a_matrix_and_persists_nothing(compare_client, temp_db):
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)
    before = _counts(SessionLocal)

    result = build_preview_matrix(
        state["project_id"], "阀门", state["confirmations"],
    )

    # 1) 真的算出来了
    assert result.matrix, "预览没有返回矩阵"
    assert result.matrix["basis"] == "preview"
    assert result.matrix.get("rows"), "矩阵没有行——链路没跑通，而不是「没有数据」"
    assert len(result.confirmed_submissions) == 2, result.notes

    # 2) 一个字节都没落库
    assert _counts(SessionLocal) == before, (
        "预览把数据写进库了——A 方案的全部安全性就在这一条上")


def test_preview_never_recommends_firmly(compare_client, temp_db):
    """契约层已经拦了（cut 1），这里验的是编排器不会构造出那种结果。"""
    state = _setup_unconfirmed(compare_client)
    result = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])
    assert result.matrix.get("recommendation_level") != "firm"
    assert result.matrix.get("comprehensive_recommendation_status") != "firm"


def test_preview_refuses_without_confirmed_tender_list(compare_client, temp_db):
    """基准不能模糊：没有已确认的采购清单就没有行轴，宁可拒绝也不替用户确认。"""
    r = compare_client.post("/api/projects", json={"name": "无清单项目", "code": "PV-NO-LIST"})
    project_id = r.json()["id"]
    job = _upload_quote(compare_client, "A.png", (255, 255, 255))

    with pytest.raises(PreviewNotReady, match="采购清单"):
        build_preview_matrix(project_id, "阀门", [
            BatchConfirmRequest(job_id=job, supplier_name="供应商A",
                                project_id=project_id, category="阀门"),
        ])


def test_one_bad_file_does_not_kill_the_whole_preview(compare_client, temp_db):
    """预览的价值是"先看个大概"，一份进不去不该让另外几份也看不成——
    但缺席必须如实列在 notes 里，不静默跳过。"""
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)
    bad = BatchConfirmRequest(job_id="does-not-exist", supplier_name="幽灵供应商",
                              project_id=state["project_id"], category="阀门")
    before = _counts(SessionLocal)

    result = build_preview_matrix(
        state["project_id"], "阀门", [*state["confirmations"], bad])

    assert len(result.confirmed_submissions) == 2
    assert any("does-not-exist" in n for n in result.notes), result.notes
    assert _counts(SessionLocal) == before


def test_repeated_previews_do_not_accumulate(compare_client, temp_db):
    """预览可以随便点。每次都从同一个真实状态出发，不会越点越脏。"""
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)
    before = _counts(SessionLocal)

    first = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])
    second = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])

    assert _counts(SessionLocal) == before
    # 同样的输入、同样的库状态 → 同样的行数。数不一样说明上一次漏了东西出去。
    assert len(first.matrix["rows"]) == len(second.matrix["rows"])


# ── 路由层（design/31 cut 2b）────────────────────────────────────────────────

def test_preview_endpoint_returns_matrix_queue_and_persists_nothing(compare_client, temp_db):
    _engine, SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)
    before = _counts(SessionLocal)

    r = compare_client.post("/api/analysis/bid-matrix/preview", json={
        "project_id": state["project_id"], "category": "阀门",
        "confirmations": [c.model_dump() for c in state["confirmations"]],
    })
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["matrix"]["basis"] == "preview"
    assert body["matrix"]["rows"]
    assert body["matrix"]["recommendation_level"] != "firm"
    assert isinstance(body["queue"], list)
    assert body["summary"]
    assert _counts(SessionLocal) == before, "预览端点把数据写进库了"


def test_preview_endpoint_409_without_tender_list(compare_client, temp_db):
    r = compare_client.post("/api/projects", json={"name": "无清单2", "code": "PV-NL2"})
    project_id = r.json()["id"]
    job = _upload_quote(compare_client, "A.png", (255, 255, 255))

    r = compare_client.post("/api/analysis/bid-matrix/preview", json={
        "project_id": project_id, "category": "阀门",
        "confirmations": [{"job_id": job, "supplier_name": "供应商A",
                           "project_id": project_id, "category": "阀门"}],
    })
    assert r.status_code == 409
    assert "采购清单" in r.json()["detail"]
