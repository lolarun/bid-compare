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
from apps.api.services.matrix.preview_service import (
    PreviewNotReady,
    build_preview_matrix,
)

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


def test_preview_falls_back_to_quote_derived_axis_without_tender_list(compare_client, temp_db):
    """design/32：没有已确认采购清单不再直接拒绝——从报价自己派生行轴。"""
    r = compare_client.post("/api/projects", json={"name": "无清单项目", "code": "PV-NO-LIST"})
    project_id = r.json()["id"]
    job = _upload_quote(compare_client, "A.png", (255, 255, 255))

    result = build_preview_matrix(project_id, "阀门", [
        BatchConfirmRequest(job_id=job, supplier_name="供应商A",
                            project_id=project_id, category="阀门"),
    ])
    assert result.matrix["axis_kind"] == "quote_derived"
    assert result.matrix["basis"] == "preview"
    assert result.matrix["rows"], "派生轴应该能算出行"
    assert any("未提供采购清单" in n for n in result.notes), result.notes


def test_preview_still_refuses_with_zero_confirmable_submissions(compare_client, temp_db):
    """派生轴解决的是"没有采购清单"，不是"没有报价"——一份报价都进不了库时
    仍然没有任何东西可以比，这条边界要继续挡住。"""
    r = compare_client.post("/api/projects", json={"name": "空项目", "code": "PV-EMPTY"})
    project_id = r.json()["id"]

    with pytest.raises(PreviewNotReady, match="没有任何报价"):
        build_preview_matrix(project_id, "阀门", [
            BatchConfirmRequest(job_id="does-not-exist", supplier_name="幽灵供应商",
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


def test_preview_endpoint_uses_quote_derived_axis_without_tender_list(compare_client, temp_db):
    r = compare_client.post("/api/projects", json={"name": "无清单2", "code": "PV-NL2"})
    project_id = r.json()["id"]
    job = _upload_quote(compare_client, "A.png", (255, 255, 255))

    r = compare_client.post("/api/analysis/bid-matrix/preview", json={
        "project_id": project_id, "category": "阀门",
        "confirmations": [{"job_id": job, "supplier_name": "供应商A",
                           "project_id": project_id, "category": "阀门"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matrix"]["axis_kind"] == "quote_derived"
    assert any("未提供采购清单" in n for n in body["notes"])


def test_preview_endpoint_409_when_no_submissions_confirm(compare_client, temp_db):
    r = compare_client.post("/api/projects", json={"name": "无清单3", "code": "PV-NL3"})
    project_id = r.json()["id"]

    r = compare_client.post("/api/analysis/bid-matrix/preview", json={
        "project_id": project_id, "category": "阀门",
        "confirmations": [{"job_id": "does-not-exist", "supplier_name": "幽灵供应商",
                           "project_id": project_id, "category": "阀门"}],
    })
    assert r.status_code == 409
    assert "没有任何报价" in r.json()["detail"]


# ── design/32 A1+A2：真的对齐了，不是每行都掉进 pending ─────────────────────

def test_quote_derived_axis_actually_aligns_rows_across_suppliers(compare_client, temp_db):
    """两家供应商在派生轴下，同一行确实被认成同一行——不是矩阵有行有列，
    但格子全是「missing/pending」这种"看起来能用、实际没对上"的假象。

    两家 mock 报价（供应商A/B）品名、规格、位置、数量逐位相同（跟 design/32
    §2 量出来的语料形状一致），这正是 A2 的判据能通过、不用退到 embedding
    的场景。"""
    r = compare_client.post("/api/projects", json={"name": "派生轴对齐项目", "code": "PV-QDA-1"})
    project_id = r.json()["id"]
    job_a = _upload_quote(compare_client, "A.png", (255, 255, 255))
    job_b = _upload_quote(compare_client, "B.png", (250, 250, 250))

    result = build_preview_matrix(project_id, "阀门", [
        BatchConfirmRequest(job_id=job_a, supplier_name="供应商A",
                            project_id=project_id, category="阀门"),
        BatchConfirmRequest(job_id=job_b, supplier_name="供应商B",
                            project_id=project_id, category="阀门"),
    ])

    assert result.matrix["axis_kind"] == "quote_derived"
    assert len(result.matrix["rows"]) == 2, "两条 mock 数据各 2 项，行数应该是 2 不是 4"
    quoted_cells = [
        c for row in result.matrix["rows"] for c in (row.get("suppliers") or [])
        if isinstance(c, dict) and c.get("cell_status") == "quoted"
    ]
    # 2 行 × 2 家 = 4 格全部对齐成功；任何一格掉进 pending 都说明位置+数量
    # 判据没通过、退到了 embedding 或整体判失败——那不是这个用例要覆盖的路径。
    assert len(quoted_cells) == 4, (
        f"应有 4 个格子对齐成功，实际 {len(quoted_cells)}——"
        f"检查 rows: {result.matrix['rows']}")


# ── design/32 §8：质量门在预览里只警告，不阻断 ─────────────────────────────

def _blow_the_checksum(client, job_id: str) -> None:
    """把 job 的声明总价改成一个跟明细对不上的数，制造 declared_total_mismatch。

    这正是 2026-08-22 手测撞到的形状：凯硕新正的合计行被当成第 90 条报价行，
    明细之和 = 真实总额 × 2，声明总价闭环门判 fail。"""
    from apps.api.core.database import SessionLocal
    from apps.api.models.extraction_job import ExtractionJob
    with SessionLocal() as s:
        job = s.get(ExtractionJob, job_id)
        res = dict(job.result or {})
        # 声明总价的唯一口径是 `_doc_meta.bid_total`（见 _declared_total）。
        # 第一版写成 res["declared_total"]，门根本不认那个键——测试"通过"了
        # 但什么也没验到。
        res["_doc_meta"] = {**(res.get("_doc_meta") or {}), "bid_total": 1.0}
        job.result = res
        s.commit()


def test_a_supplier_failing_the_checksum_gate_still_enters_preview(compare_client, temp_db):
    """一家的声明总价对不上，不该让整个预览做不成。

    用户原话：「能不能比价是一个等级，有几个能比价是另外一个等级」。
    """
    state = _setup_unconfirmed(compare_client)
    _blow_the_checksum(compare_client, state["confirmations"][0].job_id)

    result = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])

    assert len(result.confirmed_submissions) == 2, (
        f"两家都该进预览，实际 {len(result.confirmed_submissions)}；notes={result.notes}")
    assert result.matrix["rows"]
    # 降级不是放行：疑点必须如实带出来。
    assert any("疑点" in n for n in result.notes), result.notes


def test_official_path_still_blocks_on_the_same_gate(compare_client, temp_db):
    """预览放行不等于官方路径也放行——门对正式入库一点没松。"""
    from apps.api.core.database import SessionLocal
    from apps.api.core.errors import ReviewRequiredError
    from apps.api.services.submission.quote_confirmation_service import confirm_batch

    state = _setup_unconfirmed(compare_client)
    body = state["confirmations"][0]
    _blow_the_checksum(compare_client, body.job_id)

    with SessionLocal() as db:
        with pytest.raises(ReviewRequiredError):
            confirm_batch(db, body)          # gates_advisory 默认 False


# ── design/32 A1 落到入库侧：合计行不再被当成报价行 ────────────────────────

def test_aggregate_row_is_excluded_and_reported(compare_client, temp_db):
    """合计行不入库，且必须**报出来**——静默排除等于用删行让门通过。

    形状取自真实数据（凯硕新正 PDF 第 90 行）：标签串进名称/规格/单位三列。
    """
    from apps.api.core.database import SessionLocal
    from apps.api.models.extraction_job import ExtractionJob
    from apps.api.services.submission.quote_confirmation_service import confirm_batch

    state = _setup_unconfirmed(compare_client)
    body = state["confirmations"][0]

    with SessionLocal() as s:
        job = s.get(ExtractionJob, body.job_id)
        res = dict(job.result or {})
        items = list(res.get("items") or [])
        n_real = len(items)
        # copy_no 必须跟既有行一致：不一致会多出一个副本分组，
        # _dedupe_copies（design/24 B0）会把整组当成"另一份副本"丢掉，
        # 这条用例就永远测不到它想测的判据。
        _copy_no = items[-1].get("copy_no") if items else None
        items.append({
            "material": "含税合价（元）：", "spec": "含税合价（元）：",
            "unit": "含税合价（元）：", "qty": None,
            "total_price": 999999.0, "category": "阀门", "copy_no": _copy_no,
        })
        res["items"] = items
        job.result = res
        s.commit()

    with SessionLocal() as db:
        out = confirm_batch(db, body, gates_advisory=True)

    assert out["line_count"] == n_real, (
        f"合计行被当成报价行入库了：line_count={out['line_count']}，应为 {n_real}")
    assert len(out["aggregate_rows"]) == 1, out["aggregate_rows"]
    assert "含税合价" in out["aggregate_rows"][0]["label"]
    assert out["aggregate_rows"][0]["reason"]


def test_a_real_item_missing_quantity_is_still_stored(compare_client, temp_db):
    """跟上一条相反的方向：qty 丢失的真条目必须照常入库。

    实测这种行真实存在（识别串列导致 qty 丢失，但金额是真的）。判据要是写成
    "无数量即丢弃"，这里就会静默少一条报价、少一笔钱。
    """
    from apps.api.core.database import SessionLocal
    from apps.api.models.extraction_job import ExtractionJob
    from apps.api.services.submission.quote_confirmation_service import confirm_batch

    state = _setup_unconfirmed(compare_client)
    body = state["confirmations"][0]

    with SessionLocal() as s:
        job = s.get(ExtractionJob, body.job_id)
        res = dict(job.result or {})
        items = list(res.get("items") or [])
        n_real = len(items)
        _copy_no = items[-1].get("copy_no") if items else None   # 见上一条用例的说明
        items.append({
            "material": "缓闭式止回阀", "spec": "DN100", "unit": "EPDM",
            "qty": None, "total_price": 3460.0, "category": "阀门", "copy_no": _copy_no,
        })
        res["items"] = items
        job.result = res
        s.commit()

    with SessionLocal() as db:
        out = confirm_batch(db, body, gates_advisory=True)

    assert out["line_count"] == n_real + 1, "qty 丢失的真条目被误删了"
    assert out["aggregate_rows"] == []


# ── design/32 §11：待确认项要带原文依据（页/行 + 识别到的字段）────────────

def test_queue_items_carry_source_evidence(compare_client, temp_db):
    """用户问「待确认怎么确认？去看纸质版找那一行？」——不该。系统有
    source_ref，把页/行和该行识别到的字段一起带出来。

    **必须在沙箱内取**：预览的 BidQuoteLine 退出沙箱就回滚了，
    `bid_quote_line_id` 变成悬空数字，事后开接口查是查不到的。这条用例
    正是守这一点——它拿到的证据只可能来自沙箱内。
    """
    state = _setup_unconfirmed(compare_client)
    result = build_preview_matrix(state["project_id"], "阀门", state["confirmations"])

    q = result.queue
    assert q is not None, "队列应由 service 在沙箱内构建并带回"
    if q.pending_count == 0:
        pytest.skip("这批 mock 数据没有 pending 格子，证据链路由真实语料覆盖")
    for imp in q.queue:
        ev = q.evidence.get((imp.anchor_key, imp.supplier_key))
        assert ev is not None, f"待确认项没有原文依据：{imp}"
        assert "page" in ev and "row" in ev
        # 关键字段必须都在——用户是靠"哪个是空的"来判断问题出在哪。
        for k in ("raw_name", "spec", "unit", "qty", "unit_price", "total_price"):
            assert k in ev, f"证据缺字段 {k}"


def test_endpoint_exposes_evidence(compare_client, temp_db):
    state = _setup_unconfirmed(compare_client)
    r = compare_client.post("/api/analysis/bid-matrix/preview", json={
        "project_id": state["project_id"], "category": "阀门",
        "confirmations": [c.model_dump() for c in state["confirmations"]],
    })
    assert r.status_code == 200, r.text
    for item in r.json()["queue"]:
        assert "evidence" in item, "队列项没有透出 evidence 字段"
