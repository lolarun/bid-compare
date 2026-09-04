"""份级口径 API 的契约测试（P1）。

端到端跑一遍母线第一轮的真实情形：一家「不含安装」混在三家「含安装」里，
`basis-check` 必须报不可比，并说清谁跟谁不同。
"""

from __future__ import annotations

from apps.api.models.bid_submission import BidSubmission
from apps.api.models.project import Project
from apps.api.models.quote_round import QuoteRound
from apps.api.models.submission_basis import DIM_DELIVERY_SCOPE


def _seed_round(db, names: list[str]) -> tuple[int, list[int]]:
    proj = Project(name="临港中科院项目", status="active")
    db.add(proj)
    db.flush()
    rnd = QuoteRound(
        project_id=proj.id, category="母线槽", seq=1, name="第1轮",
        stage="formal", status="open",
    )
    db.add(rnd)
    db.flush()
    ids = []
    for i, n in enumerate(names):
        sub = BidSubmission(
            job_id=f"job-{i}", supplier_raw_name=n, project_id=proj.id,
            round_id=rnd.id, batch_id=f"batch-{proj.id}-{i}", status="confirmed",
        )
        db.add(sub)
        db.flush()
        ids.append(sub.id)
    db.commit()
    return rnd.id, ids


def test_put_and_list_basis(legacy_client, db_session):
    _, ids = _seed_round(db_session, ["上海都安实业"])
    sid = ids[0]

    r = legacy_client.put(
        f"/api/submissions/{sid}/basis/{DIM_DELIVERY_SCOPE}",
        json={
            "status": "confirmed",
            "value": {"scope": "excl_installation"},
            "raw_text": "不含安装",
            "source_ref": {"page": 1, "row": 1},
            "extracted_by": "manual",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    # 原文必须随值返回——界面不许只显示归一值
    assert body["raw_text"] == "不含安装"
    assert body["source_ref"] == {"page": 1, "row": 1}
    assert body["confirmed_by"]          # 记了真人，不是写死的字符串

    listed = legacy_client.get(f"/api/submissions/{sid}/basis").json()
    assert len(listed) == 1
    assert listed[0]["dim"] == DIM_DELIVERY_SCOPE


def test_list_does_not_pad_missing_dimensions(legacy_client, db_session):
    """没有的维度不补空行——"库里没这条"和"原文里没声明"不能混。"""
    _, ids = _seed_round(db_session, ["甲"])
    assert legacy_client.get(f"/api/submissions/{ids[0]}/basis").json() == []


def test_confirmed_without_value_is_rejected(legacy_client, db_session):
    """confirmed 必须带值；"原文里确实没有"该走 not_present。"""
    _, ids = _seed_round(db_session, ["甲"])
    r = legacy_client.put(
        f"/api/submissions/{ids[0]}/basis/{DIM_DELIVERY_SCOPE}",
        json={"status": "confirmed", "value": None},
    )
    assert r.status_code == 400
    assert "not_present" in r.json()["detail"]


def test_unknown_dimension_and_status_rejected(legacy_client, db_session):
    _, ids = _seed_round(db_session, ["甲"])
    sid = ids[0]
    assert legacy_client.put(
        f"/api/submissions/{sid}/basis/made_up",
        json={"status": "confirmed", "value": {"x": 1}},
    ).status_code == 400
    assert legacy_client.put(
        f"/api/submissions/{sid}/basis/{DIM_DELIVERY_SCOPE}",
        json={"status": "made_up", "value": {"x": 1}},
    ).status_code == 400


def test_round_basis_check_reports_the_real_busduct_conflict(legacy_client, db_session):
    """母线第一轮：都安「不含安装」vs 其余三家「含安装」→ 不可比。"""
    round_id, ids = _seed_round(
        db_session, ["上海都安实业", "江苏永旗电气", "上海塞克西德", "大航有能电气"],
    )
    legacy_client.put(
        f"/api/submissions/{ids[0]}/basis/{DIM_DELIVERY_SCOPE}",
        json={"status": "confirmed", "value": {"scope": "excl_installation"},
              "raw_text": "不含安装"},
    )
    for sid in ids[1:]:
        legacy_client.put(
            f"/api/submissions/{sid}/basis/{DIM_DELIVERY_SCOPE}",
            json={"status": "confirmed", "value": {"scope": "incl_installation"},
                  "raw_text": "含安装"},
        )

    out = legacy_client.get(f"/api/quote-rounds/{round_id}/basis-check").json()
    assert out["comparable"] is False
    scope = [c for c in out["conflicts"] if c["dim"] == DIM_DELIVERY_SCOPE]
    assert scope, out
    # 差异要说清谁跟谁不同，不是只说一句"不一致"
    groups = list(scope[0]["values"].values())
    assert ["上海都安实业"] in groups
    assert any(len(g) == 3 for g in groups)


def test_round_basis_check_flags_unconfirmed_as_unresolved(legacy_client, db_session):
    """模型抽了没人确认 → 未决，不能当"一致"放过（决策 2 的安全底线）。"""
    round_id, ids = _seed_round(db_session, ["甲", "乙"])
    for sid in ids:
        legacy_client.put(
            f"/api/submissions/{sid}/basis/{DIM_DELIVERY_SCOPE}",
            json={"status": "extracted", "value": {"scope": "incl_installation"},
                  "raw_text": "含安装", "extracted_by": "qwen-vl:test"},
        )

    out = legacy_client.get(f"/api/quote-rounds/{round_id}/basis-check").json()
    assert out["comparable"] is False
    assert out["conflicts"] == []
    assert len(out["unresolved"]) == 2


def test_round_basis_check_404(legacy_client):
    assert legacy_client.get("/api/quote-rounds/999999/basis-check").status_code == 404
