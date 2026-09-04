"""archive/design/45 §6 — GET /api/projects/{id}/overview（项目概述页聚合）。

这份用例守的是概述页最容易说谎的四件事：

1. **一个项目多品类时，每个品类一张卡**（D-1 / 约束 C2）。轮次、行轴、矩阵
   全部按 `(project, category)` 分域，把项目级的一个轮次号印在卡片上就是
   在对跨品类项目说谎。
2. **两个总价永远分开**（FUNCTIONAL §5）：明细合计是算出来的，文件声明总价
   是文件自己写的。合并成一个数，就把"识别是否完整"的独立证据抹掉了。
3. **行轴类型如实标注**（约束 C3）：没有已确认采购清单时是 `quote_derived`，
   而 `quote_derived` 只能进预览通道，概述页据此不出结论。
4. **概述端点不算矩阵**：它必须是便宜的只读聚合，评标结论走 D 区块懒加载
   （约束 C4）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _mk_list(db, project_id: int, category: str, *, anchors=89, confirmed=True):
    from apps.api.models.tender_list_session import TenderListSession

    s = TenderListSession(
        project_id=project_id, category=category, anchors_total=anchors,
        version=1, is_current=True, source_type="excel",
        file_name=f"{category}采购清单.xlsx",
        status="confirmed" if confirmed else "preview",
    )
    db.add(s)
    db.commit()
    return s


def _mk_quote(db, project_id, round_id, batch, *, lines, declared, supplier="某供应商"):
    """建一份已入库报价：job(lifecycle=confirmed) + submission + N 行明细。"""
    from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
    from apps.api.models.extraction_job import ExtractionJob

    job = ExtractionJob(
        id=f"job-{batch}", type="quote", status="done", lifecycle="confirmed",
        filename=f"{supplier}.pdf", context={"project_id": project_id},
        result={"_doc_meta": {"bid_total": declared}} if declared is not None else {},
    )
    db.add(job)
    db.flush()
    sub = BidSubmission(
        job_id=job.id, project_id=project_id, round_id=round_id,
        supplier_raw_name=supplier, batch_id=batch, status="pending",
    )
    db.add(sub)
    db.flush()
    for i, amount in enumerate(lines):
        db.add(BidQuoteLine(
            submission_id=sub.id, standard_name=f"物料{i}", total_price=amount,
        ))
    db.commit()
    return sub


def test_404_for_unknown_project(client):
    assert client.get("/api/projects/999999/overview").status_code == 404


def test_multi_category_gets_one_card_each(client, db_session):
    """约束 C2：跨品类项目不得被压成一个轮次号。"""
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "跨品类", "code": "PO1"}).json()
    rv = svc.create_round(db_session, proj["id"], "阀门", name="阀门第一轮")
    rc = svc.create_round(db_session, proj["id"], "电缆", name="电缆第一轮")
    db_session.commit()
    # 阀门：清单已确认 + 已入库一份报价 → 可出比价
    _mk_list(db_session, proj["id"], "阀门")
    _mk_quote(db_session, proj["id"], rv.id, "b-po1-v", lines=[10.0], declared=10.0)
    # 电缆：报价先到、清单还没确认 → 清单未确认
    _mk_quote(db_session, proj["id"], rc.id, "b-po1-c", lines=[20.0], declared=20.0)

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    cats = {c["category"]: c for c in out["categories"]}
    assert set(cats) == {"阀门", "电缆"}
    assert cats["阀门"]["has_confirmed_list"] is True
    assert cats["电缆"]["has_confirmed_list"] is False
    # 两张卡必须各说各的状态——这正是 D-1「每品类一张卡」要守住的东西
    assert cats["阀门"]["next_action"]["code"] == "ready_to_compare"
    assert cats["电缆"]["next_action"]["code"] == "list_unconfirmed"
    assert cats["阀门"]["axis_kind"] == "tender_anchor"
    assert cats["电缆"]["axis_kind"] == "quote_derived"


def test_the_two_totals_stay_separate(client, db_session):
    """FUNCTIONAL §5：明细合计（算的） vs 文件声明总价（文件写的），不得合并。"""
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "两个总价", "code": "PO2"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_list(db_session, proj["id"], "阀门")
    # 明细合计 100+200+300=600，而文件封面声明 650——真实文档里这种差额正是
    # checksum 要抓的东西，概述页必须两个数都给出来，让人看得见差。
    _mk_quote(db_session, proj["id"], r.id, "b-po2", lines=[100.0, 200.0, 300.0],
              declared=650.0, supplier="甲供应商")

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    sup = out["categories"][0]["suppliers"][0]
    assert sup["line_count"] == 3
    assert sup["detail_total"] == 600.0
    assert sup["declared_total"] == 650.0
    assert sup["supplier_name"] == "甲供应商"


def test_declared_total_is_none_when_the_document_never_stated_one(client, db_session):
    """没有声明总价 ≠ 声明为 0。缺失必须是 null，不能被填成一个数。"""
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "无声明", "code": "PO3"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_list(db_session, proj["id"], "阀门")
    _mk_quote(db_session, proj["id"], r.id, "b-po3", lines=[10.0], declared=None)

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    sup = out["categories"][0]["suppliers"][0]
    assert sup["declared_total"] is None
    assert sup["detail_total"] == 10.0


def test_axis_kind_is_quote_derived_without_a_confirmed_list(client, db_session):
    """约束 C3：没有已确认清单时行轴是报价派生轴，概述页据此不出结论。"""
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "无清单", "code": "PO4"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_quote(db_session, proj["id"], r.id, "b-po4", lines=[10.0], declared=10.0)

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    cat = out["categories"][0]
    assert cat["axis_kind"] == "quote_derived"
    assert cat["list"] is None


def test_axis_kind_is_tender_anchor_with_a_confirmed_list(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "有清单", "code": "PO5"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_list(db_session, proj["id"], "阀门", anchors=89)
    _mk_quote(db_session, proj["id"], r.id, "b-po5", lines=[10.0], declared=10.0)

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    cat = out["categories"][0]
    assert cat["axis_kind"] == "tender_anchor"
    assert cat["list"]["anchor_count"] == 89
    assert cat["list"]["source_type"] == "excel"


def test_unconfirmed_list_does_not_count_as_confirmed(client, db_session):
    """`is_current` 与 `status='confirmed'` 两个条件都要——闸门不得放松。"""
    proj = client.post("/api/projects", json={"name": "预览清单", "code": "PO6"}).json()
    _mk_list(db_session, proj["id"], "阀门", confirmed=False)

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    cat = next(c for c in out["categories"] if c["category"] == "阀门")
    assert cat["has_confirmed_list"] is False
    assert cat["list"] is None


def test_rounds_carry_their_own_quote_lists(client, db_session):
    """D-2：历史轮次给报价清单，不给结论。

    这里断言的是"每轮各自带自己的 submissions"——概述端点本身不产出任何
    排名/推荐字段，历史轮次因此**没有结论可显示**，是结构上做不到，而不是
    前端选择不渲染。
    """
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "两轮", "code": "PO7"}).json()
    r1 = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_list(db_session, proj["id"], "阀门")
    _mk_quote(db_session, proj["id"], r1.id, "b-po7-1", lines=[100.0], declared=100.0,
              supplier="甲")
    r2 = svc.create_round(db_session, proj["id"], "阀门", name="第二轮")
    db_session.commit()
    _mk_quote(db_session, proj["id"], r2.id, "b-po7-2", lines=[90.0], declared=90.0,
              supplier="乙")

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    cat = out["categories"][0]
    rounds = {r["seq"]: r for r in cat["rounds"]}
    assert [s["supplier_name"] for s in rounds[1]["submissions"]] == ["甲"]
    assert [s["supplier_name"] for s in rounds[2]["submissions"]] == ["乙"]
    assert rounds[1]["status"] == "closed"      # 开新轮会关掉上一轮
    assert rounds[2]["status"] == "open"
    assert cat["current_round"]["seq"] == 2

    # 概述端点不得携带任何评标结论字段（约束 C4：结论只从 bid-matrix 来）
    forbidden = {"recommendation_level", "evaluated_total", "ranking", "recommended_supplier"}
    assert not (forbidden & set(cat)), f"概述端点长出了结论字段：{forbidden & set(cat)}"
    for r in cat["rounds"]:
        assert not (forbidden & set(r))


def test_superseded_submissions_are_excluded(client, db_session):
    """与 /compare-state 同一道过滤：superseded/rejected 不算数。"""
    from apps.api.models.bid_submission import BidSubmission
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "有废弃", "code": "PO8"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_list(db_session, proj["id"], "阀门")
    old = _mk_quote(db_session, proj["id"], r.id, "b-po8-old", lines=[1.0], declared=1.0,
                    supplier="旧件")
    _mk_quote(db_session, proj["id"], r.id, "b-po8-new", lines=[2.0], declared=2.0,
              supplier="新件")
    db_session.get(BidSubmission, old.id).status = "superseded"
    db_session.commit()

    out = client.get(f"/api/projects/{proj['id']}/overview").json()
    names = [s["supplier_name"] for s in out["categories"][0]["suppliers"]]
    assert names == ["新件"]
    assert out["categories"][0]["submission_count"] == 1


def test_overview_route_is_not_shadowed_by_the_project_detail_route(client, db_session):
    """`/{project_id}/overview` 必须排在 `/{project_id}` 之前，否则被吃掉。"""
    proj = client.post("/api/projects", json={"name": "路由顺序", "code": "PO9"}).json()
    resp = client.get(f"/api/projects/{proj['id']}/overview")
    assert resp.status_code == 200
    assert "categories" in resp.json()      # 不是 ProjectOut 的形状
