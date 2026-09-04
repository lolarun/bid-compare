"""design/24 B3 —— batch-confirm dry_run 收集器化回归测试。

修复前：结构完整性/原文无合价/全部跳过/声明总价四道门 fail-fast，命中第一道
就 422+rollback，用户得来回提交好几次才依次看全所有问题。这轮改造让
dry_run=True 时四道门变"收集不阻断"，一次报出全部疑点；dry_run=False（真实
写入路径）逐字节行为不变——仍然命中第一道就停。

三层测试：
  A. 真实路径 fail-fast 行为验证（改动前后不变的锚点）。
  B. dry_run 一次性收集多个疑点 + 从不写库。
  C. 双向契约：dry_run 报的每条 issue 与真实路径逐个 raise 出来的 payload
     同形同值（Fable 评审明确要求）。
"""
from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from apps.api.models import BidQuoteLine, BidSubmission

# 同时踩两道门：1/2 行完全重复（duplicate_amount_ratio 超阈值 → BLOCKED），
# 3 行没有 total_price（missing_total_requires_review）。
DUAL_ISSUE_QUOTE = {
    "supplier_name": "宏胜电缆",
    "quote_date": "2026-08-01",
    "items": [
        {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
         "unit_price": 50, "total_price": 5000},
        {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
         "unit_price": 50, "total_price": 5000},
        {"material": "YJV-3*70", "spec": "0.6/1kV", "unit": "米", "qty": 200,
         "unit_price": 40},  # 无 total_price → missing_total_rows
    ],
}

CLEAN_QUOTE = {
    "supplier_name": "远东电缆",
    "quote_date": "2026-08-01",
    "items": [
        {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
         "unit_price": 50, "total_price": 5000},
    ],
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


def _mk_client(temp_db, monkeypatch, tmp_path, canned: dict):
    from apps.api.intelligence.base import ExtractionResponse
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider

    class _Provider(MockProvider):
        def extract(self, images, schema, prompt, timeout=90, **kwargs):
            return ExtractionResponse(
                data=canned, raw_text=json.dumps(canned, ensure_ascii=False),
                confidence=1.0, tokens_used=0, provider="mock-dryrun", duration_ms=1,
            )

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )
    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(_Provider()),
    )
    from apps.api.main import app
    return TestClient(app)


def _upload(c, filename="宏胜.png") -> str:
    r = c.post(
        "/api/intake/upload",
        data={"type": "quote", "category": "电缆", "project_id": ""},
        files={"file": (filename, _png(), "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ── A. 真实路径 fail-fast 行为不变 ───────────────────────────────────────────

def test_real_path_still_fails_fast_on_first_gate(temp_db, monkeypatch, tmp_path, auth_override):
    """真实写入路径命中结构完整性门就停——不会因为这轮改造变成一次性报告。"""
    with _mk_client(temp_db, monkeypatch, tmp_path, DUAL_ISSUE_QUOTE) as c:
        job_id = _upload(c)
        r = c.post("/api/quotes/batch-confirm", json={
            "job_id": job_id, "supplier_name": "宏胜电缆", "category": "电缆",
        })
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "structural_integrity_requires_review"
        # 只报了第一道门——missing_total 那道门此刻还没轮到，压根没出现在响应里
        assert "review_row_count" in detail
        assert "missing" not in json.dumps(detail).lower() or detail["error"] != "missing_total_requires_review"


# ── B. dry_run 一次性收集 + 从不写库 ─────────────────────────────────────────

class TestDryRunCollectsEverything:
    def test_collects_both_issues_in_one_call(self, temp_db, monkeypatch, tmp_path, auth_override):
        with _mk_client(temp_db, monkeypatch, tmp_path, DUAL_ISSUE_QUOTE) as c:
            job_id = _upload(c)
            r = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_id, "supplier_name": "宏胜电缆", "category": "电缆",
                "dry_run": True,
            })
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["dry_run"] is True
            assert data["would_succeed"] is False
            error_codes = {issue["error"] for issue in data["issues"]}
            assert "structural_integrity_requires_review" in error_codes
            assert "missing_total_requires_review" in error_codes
            assert len(data["issues"]) >= 2

    def test_dry_run_never_writes_to_db(self, temp_db, monkeypatch, tmp_path, auth_override, db_session):
        with _mk_client(temp_db, monkeypatch, tmp_path, DUAL_ISSUE_QUOTE) as c:
            job_id = _upload(c)
            r = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_id, "supplier_name": "宏胜电缆", "category": "电缆",
                "dry_run": True,
            })
            assert r.status_code == 200, r.text

        # 独立 session 直查——dry-run 声称"要写 3 行"，但库里必须一行都没有。
        subs = db_session.scalars(
            select(BidSubmission).where(BidSubmission.batch_id == f"BID-{job_id}")
        ).all()
        assert subs == [], f"dry_run 不该留下任何 BidSubmission，实际：{subs}"
        lines = db_session.scalars(select(BidQuoteLine)).all()
        assert lines == [], f"dry_run 不该写入任何 BidQuoteLine，实际：{len(lines)} 行"

    def test_clean_document_would_succeed_and_still_writes_nothing(
        self, temp_db, monkeypatch, tmp_path, auth_override, db_session,
    ):
        with _mk_client(temp_db, monkeypatch, tmp_path, CLEAN_QUOTE) as c:
            job_id = _upload(c, "远东.png")
            r = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_id, "supplier_name": "远东电缆", "category": "电缆",
                "dry_run": True,
            })
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["would_succeed"] is True
            assert data["issues"] == []
            assert data["line_count"] == 1   # 预告"会写 1 行"，但没真写

        subs = db_session.scalars(
            select(BidSubmission).where(BidSubmission.batch_id == f"BID-{job_id}")
        ).all()
        assert subs == []

    def test_second_identical_dry_run_call_hits_cache(
        self, temp_db, monkeypatch, tmp_path, auth_override,
    ):
        """design/24 B4：同一份文档、同样的请求，第二次 dry_run 不再重新跑
        _gate_integrity——直接命中缓存返回。"""
        import apps.api.services.submission.quote_confirmation_service as svc

        calls = {"n": 0}
        real_gate = svc._gate_integrity

        def counting_gate(*a, **kw):
            calls["n"] += 1
            return real_gate(*a, **kw)

        monkeypatch.setattr(svc, "_gate_integrity", counting_gate)

        with _mk_client(temp_db, monkeypatch, tmp_path, CLEAN_QUOTE) as c:
            job_id = _upload(c, "cache.png")
            payload = {
                "job_id": job_id, "supplier_name": "远东电缆", "category": "电缆",
                "dry_run": True,
            }
            r1 = c.post("/api/quotes/batch-confirm", json=payload)
            r2 = c.post("/api/quotes/batch-confirm", json=payload)
            assert r1.status_code == 200 and r2.status_code == 200
            assert r1.json() == r2.json()
            assert calls["n"] == 1, f"第二次调用应该命中缓存，不该再跑门禁；实际跑了 {calls['n']} 次"

    def test_real_confirm_after_dry_run_is_a_genuine_first_write(
        self, temp_db, monkeypatch, tmp_path, auth_override,
    ):
        """dry_run 之后紧接着真实确认：必须是"首次入库"，不能被 dry_run 的残留
        误判成幂等命中（idempotent=True 不该出现）。"""
        with _mk_client(temp_db, monkeypatch, tmp_path, CLEAN_QUOTE) as c:
            job_id = _upload(c, "远东.png")
            r1 = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_id, "supplier_name": "远东电缆", "category": "电缆",
                "dry_run": True,
            })
            assert r1.status_code == 200, r1.text

            r2 = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_id, "supplier_name": "远东电缆", "category": "电缆",
            })
            assert r2.status_code == 200, r2.text
            data = r2.json()
            assert data.get("idempotent") is not True
            assert data["line_count"] == 1


# ── C. 双向契约：dry_run 的 issue 与真实路径逐个 raise 的 payload 同形同值 ────

class TestDryRunMatchesRealRaises:
    def test_integrity_issue_matches_real_422_payload(
        self, temp_db, monkeypatch, tmp_path, auth_override,
    ):
        """同一份文档：dry_run 报的第一个 issue（结构完整性）应与真实路径 422
        的 detail 逐字段一致——不是"看起来差不多"，是同一份 payload 构造代码。
        """
        with _mk_client(temp_db, monkeypatch, tmp_path, DUAL_ISSUE_QUOTE) as c:
            job_dry = _upload(c, "dry.png")
            r_dry = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_dry, "supplier_name": "宏胜电缆", "category": "电缆",
                "dry_run": True,
            })
            dry_issues = {i["error"]: i for i in r_dry.json()["issues"]}

        with _mk_client(temp_db, monkeypatch, tmp_path, DUAL_ISSUE_QUOTE) as c:
            job_real = _upload(c, "real.png")
            r_real = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_real, "supplier_name": "宏胜电缆", "category": "电缆",
            })
            assert r_real.status_code == 422
            real_detail = r_real.json()["detail"]

        assert real_detail["error"] == "structural_integrity_requires_review"
        dry_integrity_issue = dry_issues["structural_integrity_requires_review"]
        # review_rows 里的 index 依赖各自 job 的行序，本身应一致（两边喂的是
        # 同一份 canned 数据）；核心字段逐一比对。
        for key in ("error", "message", "review_row_count", "duplicates", "arithmetic"):
            assert dry_integrity_issue[key] == real_detail[key], (
                f"{key} 不一致：dry_run={dry_integrity_issue[key]!r} "
                f"vs 真实路径={real_detail[key]!r}"
            )

    def test_checksum_issue_matches_real_422_payload(
        self, temp_db, monkeypatch, tmp_path, auth_override,
    ):
        """单独构造一份只踩 checksum 门的文档，验证 checksum issue 的双向一致性
        （结构完整性/missing_total 都干净，只留 checksum 一道门可比）。
        """
        checksum_quote = {
            "supplier_name": "亨通电缆",
            "quote_date": "2026-08-01",
            "items": [
                {"material": "YJV-3*95", "spec": "0.6/1kV", "unit": "米", "qty": 100,
                 "unit_price": 50, "total_price": 5000},
            ],
        }
        # declared_total 通过 job.result["_doc_meta"] 注入——mock 链路本身不产
        # 声明总价，这里直接后置改 job.result 来触发 checksum 门。
        with _mk_client(temp_db, monkeypatch, tmp_path, checksum_quote) as c:
            job_dry = _upload(c, "cksum_dry.png")
            _inject_declared_total(c, job_dry, 999999.0)
            r_dry = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_dry, "supplier_name": "亨通电缆", "category": "电缆",
                "dry_run": True,
            })
            assert r_dry.status_code == 200, r_dry.text
            dry_issues = {i["error"]: i for i in r_dry.json()["issues"]}
            assert "declared_total_mismatch" in dry_issues

        with _mk_client(temp_db, monkeypatch, tmp_path, checksum_quote) as c:
            job_real = _upload(c, "cksum_real.png")
            _inject_declared_total(c, job_real, 999999.0)
            r_real = c.post("/api/quotes/batch-confirm", json={
                "job_id": job_real, "supplier_name": "亨通电缆", "category": "电缆",
            })
            assert r_real.status_code == 422, r_real.text
            real_detail = r_real.json()["detail"]

        assert real_detail["error"] == "declared_total_mismatch"
        dry_issue = dry_issues["declared_total_mismatch"]
        for key in ("error", "message", "checksum"):
            assert dry_issue[key] == real_detail[key]


def _inject_declared_total(client: TestClient, job_id: str, bid_total: float) -> None:
    """测试专用：直接改 job.result._doc_meta，绕开完整的招标封面识别链路
    （那条链路不是本轮测试对象，checksum 门的输入契约已经由其他测试覆盖）。
    """
    from apps.api.core.database import SessionLocal
    from apps.api.models import ExtractionJob

    db = SessionLocal()
    try:
        job = db.get(ExtractionJob, job_id)
        job.result = {**(job.result or {}), "_doc_meta": {"bid_total": bid_total}}
        db.add(job)
        db.commit()
    finally:
        db.close()
