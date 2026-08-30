"""docs/design/44 §3.2 + design/45 §4 — GET /api/projects/overview.

The比价入口列表 read-only aggregate: one row per project, one entry per
category. Locks the things a reader of the endpoint needs to trust:
multi-category projects get one summary per category (not one merged/
overwritten summary), the empty-project filter is semantic rather than
name-based, and the next-action label is a state readout.

**Contract change, design/45 D-3 (2026-08-30)**: an entirely empty project
is no longer returned by default. `test_project_with_no_rounds_*` below was
rewritten rather than deleted — it now pins both directions of the new
`include_empty` switch. The old single-direction assertion is preserved as
the `include_empty=true` half, so nothing that used to be guaranteed became
unguarded.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def test_empty_project_is_hidden_by_default(client):
    resp = client.post("/api/projects", json={"name": "尚无轮次的项目", "code": "OV1"})
    assert resp.status_code == 201

    out = client.get("/api/projects/overview").json()
    assert all(i["project"]["code"] != "OV1" for i in out["items"])


def test_empty_project_is_returned_with_include_empty(client):
    assert client.post(
        "/api/projects", json={"name": "尚无轮次的项目", "code": "OV1"}
    ).status_code == 201

    out = client.get("/api/projects/overview", params={"include_empty": "true"}).json()
    row = next(i for i in out["items"] if i["project"]["code"] == "OV1")
    assert row["categories"] == []


def test_total_matches_the_applied_filter(client, db_session):
    """分页 total 必须跟过滤口径一致——否则页码算得出、页里却是空的。"""
    from apps.api.services.tender import quote_round_service as svc

    kept = client.post("/api/projects", json={"name": "有轮次", "code": "OVT1"}).json()
    client.post("/api/projects", json={"name": "空壳一", "code": "OVT2"})
    client.post("/api/projects", json={"name": "空壳二", "code": "OVT3"})
    svc.create_round(db_session, kept["id"], "阀门", name="第一轮")
    db_session.commit()

    filtered = client.get("/api/projects/overview").json()
    unfiltered = client.get(
        "/api/projects/overview", params={"include_empty": "true"}
    ).json()

    assert filtered["total"] == len(filtered["items"])
    assert unfiltered["total"] == filtered["total"] + 2


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


# ─── design/45 §4.3 下一步动作 + §4.4 空项目判据 ──────────────────────────


def _mk_confirmed_list(db, project_id: int, category: str):
    """建一个「当前且已确认」的采购清单会话。

    两个条件都要（is_current + status='confirmed'）——这是全仓一致的闸门，
    只满足其一的会话不算数。
    """
    from apps.api.models.tender_list_session import TenderListSession

    s = TenderListSession(
        project_id=project_id, category=category, anchors_total=89,
        version=1, is_current=True, status="confirmed",
    )
    db.add(s)
    db.commit()
    return s


def _mk_submission(db, project_id: int, round_id: int, batch_id: str):
    from apps.api.models.bid_submission import BidSubmission
    from apps.api.models.extraction_job import ExtractionJob

    job = ExtractionJob(
        id=f"job-{batch_id}", type="quote", status="done", lifecycle="confirmed",
        filename="q.pdf", context={"project_id": project_id},
    )
    db.add(job)
    db.flush()
    sub = BidSubmission(
        job_id=job.id, project_id=project_id, round_id=round_id,
        supplier_raw_name="某供应商", batch_id=batch_id, status="pending",
    )
    db.add(sub)
    db.commit()
    return sub


def test_next_action_pending_upload_when_only_a_list_exists(client, db_session):
    proj = client.post("/api/projects", json={"name": "只有清单", "code": "NA1"}).json()
    _mk_confirmed_list(db_session, proj["id"], "阀门")

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "NA1")
    # 清单会话带来的品类：还没有任何轮次，但卡片必须显示出来
    cat = next(c for c in row["categories"] if c["category"] == "阀门")
    assert cat["current_round"] is None
    assert cat["has_confirmed_list"] is True
    assert cat["next_action"]["code"] == "pending_upload"


def test_next_action_list_unconfirmed_when_quotes_arrived_first(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "先传报价", "code": "NA2"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_submission(db_session, proj["id"], r.id, "b-na2")

    out = client.get("/api/projects/overview").json()
    cat = next(
        c for i in out["items"] if i["project"]["code"] == "NA2"
        for c in i["categories"]
    )
    assert cat["submission_count"] == 1
    assert cat["has_confirmed_list"] is False
    assert cat["next_action"]["code"] == "list_unconfirmed"


def test_next_action_ready_to_compare_then_basis_set(client, db_session):
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "齐活", "code": "NA3"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_confirmed_list(db_session, proj["id"], "阀门")
    _mk_submission(db_session, proj["id"], r.id, "b-na3")

    out = client.get("/api/projects/overview").json()
    cat = next(
        c for i in out["items"] if i["project"]["code"] == "NA3" for c in i["categories"]
    )
    assert cat["next_action"]["code"] == "ready_to_compare"

    svc.set_final_basis(db_session, r.id, True)
    out = client.get("/api/projects/overview").json()
    cat = next(
        c for i in out["items"] if i["project"]["code"] == "NA3" for c in i["categories"]
    )
    assert cat["next_action"]["code"] == "basis_set"
    assert "第1轮" in cat["next_action"]["label"]


def test_pending_intake_counts_unconfirmed_quote_jobs(client, db_session):
    """待校对 = job.lifecycle='active'，不是 BidSubmission.status。

    2026-08-30 实测：`confirm_batch` 建 submission 时写死 status='pending'
    且此后不改，所以拿 submission.status 当"待校对"判据会让每个项目都显示
    待校对。这条用例把口径钉在 job 的 lifecycle 上。
    """
    from apps.api.models.extraction_job import ExtractionJob
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post("/api/projects", json={"name": "有在途", "code": "NA4"}).json()
    r = svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()
    _mk_confirmed_list(db_session, proj["id"], "阀门")
    _mk_submission(db_session, proj["id"], r.id, "b-na4")   # 已入库（lifecycle=confirmed）
    db_session.add(ExtractionJob(                            # 待校对（lifecycle=active）
        id="job-na4-active", type="quote", status="done", lifecycle="active",
        filename="pending.pdf", context={"project_id": proj["id"]},
    ))
    db_session.commit()

    out = client.get("/api/projects/overview").json()
    row = next(i for i in out["items"] if i["project"]["code"] == "NA4")
    assert row["pending_intake_count"] == 1
    cat = row["categories"][0]
    assert cat["submission_count"] == 1
    assert cat["next_action"]["code"] == "pending_intake"
    assert cat["next_action"]["count"] == 1


def test_empty_filter_is_semantic_not_name_based(client, db_session):
    """名字长得像自动生成的、但有数据的项目，不得被过滤掉。"""
    from apps.api.services.tender import quote_round_service as svc

    proj = client.post(
        "/api/projects", json={"name": "新比价项目-1787756525344", "code": "NA5"}
    ).json()
    svc.create_round(db_session, proj["id"], "阀门", name="第一轮")
    db_session.commit()

    out = client.get("/api/projects/overview").json()
    assert any(i["project"]["code"] == "NA5" for i in out["items"])


PLACEHOLDER_NAME_PREFIX = "新比价" + "项目-"   # 拼接：这份文件自身也要能通过下面的扫描

# 这个占位名唯一合法的**产生**点：工作台首次拖入文件时惰性建项目
# （`ensureProject()`）。它正是 61 个空壳的来源——design/45 §7 记了数，D-3 选择
# 「默认过滤 + 手工删除」治标，根因修复（不再惰性建项目）在 §9 明确列为本轮
# 范围外。这里放行它、并只放行它：再出现第二个产生点或任何**按该名字过滤**的
# 判据，这条用例都会红。
PLACEHOLDER_NAME_PRODUCER = "apps/www/src/views/compare/WorkspaceView.vue"


def _python_string_literals(text: str) -> list[str]:
    """返回文件里所有**非文档字符串**的字符串字面量。

    注释根本不进 AST，所以自动排除；docstring 显式剔除。要拦的是"拿这个模式
    当判据"（它只可能出现在字符串字面量/正则里），不是"在说明里提到它"——
    解释「为什么不按名字过滤」的注释本身是有价值的，把它一起判死会逼人写出
    含糊其辞的注释。
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _strip_js_comments(text: str) -> str:
    import re

    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)      # /* 块注释 */
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)     # <!-- 模板注释 -->
    text = re.sub(r"(?m)^\s*//.*$", "", text)              # // 整行注释
    return text


def test_no_application_code_matches_the_placeholder_name_pattern():
    """design/45 §10 验收：占位项目名模式不得成为应用代码里的判据。

    一次性的数据清理脚本可以按名字选目标（有人把关、跑一次就完），但应用
    代码里一旦按名字过滤，它就成了长期判据——用户合法地这么命名就会被吞掉。
    空项目的判据必须是语义上的"空"（design/45 §4.4）。
    """
    import pathlib

    api_root = pathlib.Path(__file__).resolve().parents[1]          # apps/api
    www_src = api_root.parent / "www" / "src"
    repo = api_root.parents[1]

    hits: list[str] = []
    for p in api_root.rglob("*.py"):
        if "__pycache__" in p.parts or "tests" in p.parts:
            continue
        literals = _python_string_literals(p.read_text(encoding="utf-8"))
        if any(PLACEHOLDER_NAME_PREFIX in s for s in literals):
            hits.append(str(p.relative_to(repo)))

    for pattern in ("*.ts", "*.vue"):
        for p in www_src.rglob(pattern):
            if "node_modules" in p.parts or "__tests__" in p.parts:
                continue
            rel = p.relative_to(repo).as_posix()
            if rel == PLACEHOLDER_NAME_PRODUCER:
                continue
            if PLACEHOLDER_NAME_PREFIX in _strip_js_comments(p.read_text(encoding="utf-8")):
                hits.append(rel)

    assert hits == [], f"应用代码把占位项目名当判据了：{hits}"


def test_the_placeholder_name_producer_is_still_the_only_one():
    """放行清单本身要有人守：产生点只能有一个，且必须还在那儿。

    单独一条用例，是为了让"放行"和"断言"分开失败——如果哪天惰性建项目被移除
    了（design/45 §9 记的根因修复），这条会红，提醒把上面的放行一并删掉，而不是
    留一条永远命中不了的死放行。
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[3]
    producer = repo / PLACEHOLDER_NAME_PRODUCER
    assert producer.is_file(), f"产生点文件不在了：{PLACEHOLDER_NAME_PRODUCER}"
    assert PLACEHOLDER_NAME_PREFIX in _strip_js_comments(
        producer.read_text(encoding="utf-8")
    ), "惰性建项目的占位名没了——请同步删除上面的放行项"
