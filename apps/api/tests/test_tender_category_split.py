"""端到端：品类识别 + 多品类拆 session + 自动落 session 堵漏洞。

覆盖根因场景：
- 给排水专业 + 阀门品名 → detected_category=阀门（不是给排水）
- preview 返回 category_breakdown / unknown_count
- confirm 单品类 → 1 session；多品类 → N session，各自只含本品类锚点
- "给排水" 这类专业不会成为 session 的 category
"""

from __future__ import annotations

import io

import openpyxl
import pytest
from fastapi.testclient import TestClient


def _make_xlsx(rows: list[tuple]) -> bytes:
    """构造带规范表头(序号/专业/名称/规格/单位/数量)的清单 xlsx。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "专业", "名称", "规格", "单位", "数量"])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from apps.api.main import app
    from apps.api.routes.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "role": "管理员"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)


# ── preview：专业=给排水 但品名是阀门 → 识别为阀门 ───────────────────────
def test_preview_detects_valve_not_profession(client):
    xlsx = _make_xlsx([
        (1, "给排水", "Y型过滤器", "DN50", "个", 1),
        (2, "给排水", "冲洗取水阀（设置锁定装置）", "DN25", "个", 2),
        (3, "给排水", "减压型倒流防止器", "DN25", "个", 1),
    ])
    r = client.post(
        "/api/analysis/tender-list/preview",
        files={"file": ("阀门招标清单.xlsx", xlsx,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["detected_category"] == "阀门"           # 不是 给排水
    assert data["category_breakdown"] == {"阀门": 3}
    assert data["has_multiple_categories"] is False
    assert data["unknown_count"] == 0
    # profession 仍保留(展示用)，但每项 category 是识别结果
    assert data["items"][0]["profession"] == "给排水"
    assert data["items"][0]["category"] == "阀门"


def test_preview_multi_category_breakdown(client):
    xlsx = _make_xlsx([
        (1, "给排水", "截止阀", "DN25", "个", 1),
        (2, "给排水", "球阀", "DN20", "个", 1),
        (3, "给排水", "薄壁不锈钢管", "DN65", "米", 10),
        (4, "电气", "镀锌电缆桥架", "200x100", "米", 5),
    ])
    r = client.post(
        "/api/analysis/tender-list/preview",
        files={"file": ("混合清单.xlsx", xlsx,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["has_multiple_categories"] is True
    assert data["category_breakdown"] == {"阀门": 2, "不锈钢管": 1, "桥架": 1}
    assert data["detected_category"] == "阀门"   # 多数派


# ── confirm：单品类 1 session，多品类 N session ──────────────────────────
def _anchor(seq, name, category):
    return {"seq": str(seq), "name": name, "spec": "DN25", "category": category}


def test_confirm_single_category_one_session(client, db_session):
    from apps.api.models.tender_list_session import TenderListSession
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": 999, "category": "阀门", "file_name": "v.xlsx",
        "anchors_json": [_anchor(1, "截止阀", "阀门"), _anchor(2, "球阀", "阀门")],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["multi_category"] is False
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["category"] == "阀门"

    rows = db_session.query(TenderListSession).filter(
        TenderListSession.project_id == 999).all()
    assert len(rows) == 1
    assert rows[0].category == "阀门"
    assert rows[0].is_current is True
    assert rows[0].status == "confirmed"


def test_confirm_multi_category_splits_sessions(client, db_session):
    from apps.api.models.tender_list_session import TenderListSession
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": 998, "category": "阀门", "file_name": "mix.xlsx",
        "anchors_json": [
            _anchor(1, "截止阀", "阀门"),
            _anchor(2, "球阀", "阀门"),
            _anchor(3, "薄壁不锈钢管", "不锈钢管"),
        ],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["multi_category"] is True
    cats = {s["category"]: s["anchors_total"] for s in data["sessions"]}
    assert cats == {"阀门": 2, "不锈钢管": 1}

    # DB: 两个 session，各自只含本品类锚点；不存在 category="给排水"
    rows = db_session.query(TenderListSession).filter(
        TenderListSession.project_id == 998).all()
    by_cat = {s.category: s for s in rows}
    assert set(by_cat) == {"阀门", "不锈钢管"}
    assert len(by_cat["阀门"].anchors_json) == 2
    assert len(by_cat["不锈钢管"].anchors_json) == 1
    assert all(a["category"] == "阀门" for a in by_cat["阀门"].anchors_json)


def test_confirm_unknown_blocked_without_force(client):
    """force=False(默认)时，unknown 品类的锚点触发 400，不得静默回退。"""
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": 997, "category": "阀门", "file_name": "u.xlsx",
        "anchors_json": [
            _anchor(1, "截止阀", "阀门"),
            {"seq": "2", "name": "施工措施费", "spec": "", "category": ""},  # unknown
        ],
    })
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["detail"]["error"] == "unknown_categories"
    assert body["detail"]["unknown_count"] == 1


def test_confirm_unknown_with_force_writes_audit(client, db_session):
    """force=True 时 unknown 项归入默认品类，并写入 _category_forced=True 审计标记。"""
    from apps.api.models.tender_list_session import TenderListSession
    r = client.post("/api/analysis/tender-list/confirm", json={
        "project_id": 996, "category": "阀门", "file_name": "u.xlsx",
        "force": True,
        "anchors_json": [
            _anchor(1, "截止阀", "阀门"),
            {"seq": "2", "name": "施工措施费", "spec": "", "category": ""},  # unknown
        ],
    })
    assert r.status_code == 200, r.text
    rows = db_session.query(TenderListSession).filter(
        TenderListSession.project_id == 996).all()
    assert len(rows) == 1
    assert rows[0].category == "阀门"
    assert len(rows[0].anchors_json) == 2   # unknown 项归入默认品类
    forced = [a for a in rows[0].anchors_json if a.get("_category_forced")]
    assert len(forced) == 1  # 施工措施费标记为强制归入
