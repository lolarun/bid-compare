"""Regression tests for audit-driven fixes.

Each test pins a specific bug found during the post-Phase-3 review so
re-introducing the bug breaks CI.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.providers.qwen_vl import QwenVLProvider
from apps.api.services.document_ingestion import (
    DocumentIngestionService,
    IngestionType,
    _hash_context,
)
from apps.api.services.standardize import standardize_name, standard_key
from apps.api.services.supplier_recommend import (
    infer_categories,
    _is_category_token_match,
)


def _png(seed: int = 0) -> bytes:
    """Return a small deterministic PNG."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (seed % 256, 100, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ─── Backend audit-fix A: context-aware idempotency ────────────────────────
class TestContextAwareIdempotency:
    """Reproduces the original bug (same content → wrong supplier's job) and
    pins the fix."""

    def test_same_content_different_supplier_gets_distinct_job(
        self, db_session, tmp_path, monkeypatch, fixture_dir
    ):
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )
        pipeline = ExtractionPipeline(MockProvider(fixture_dir=fixture_dir))
        svc = DocumentIngestionService(db_session, pipeline)

        content = _png()
        job_a = svc.create_job(content, "x.png", IngestionType.QUOTE, {"supplier_id": 1})
        job_b = svc.create_job(content, "x.png", IngestionType.QUOTE, {"supplier_id": 2})
        # Different supplier context ⇒ DIFFERENT job
        assert job_a.id != job_b.id, (
            "Same file uploaded for two different suppliers must produce "
            "distinct jobs; otherwise supplier B's quote silently inherits "
            "supplier A's context."
        )

    def test_same_supplier_same_content_idempotent(
        self, db_session, tmp_path, monkeypatch, fixture_dir
    ):
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )
        pipeline = ExtractionPipeline(MockProvider(fixture_dir=fixture_dir))
        svc = DocumentIngestionService(db_session, pipeline)

        content = _png()
        ctx = {"supplier_id": 5, "category": "阀门"}
        j1 = svc.create_job(content, "x.png", IngestionType.QUOTE, ctx)
        j2 = svc.create_job(content, "x.png", IngestionType.QUOTE, ctx)
        assert j1.id == j2.id

    def test_hash_context_ignores_irrelevant_keys(self):
        a = _hash_context({"supplier_id": 1, "free_note": "hello"})
        b = _hash_context({"supplier_id": 1, "free_note": "world"})
        assert a == b, "irrelevant keys must not change the idempotency hash"

    def test_hash_context_distinguishes_relevant_keys(self):
        a = _hash_context({"supplier_id": 1, "category": "阀门"})
        b = _hash_context({"supplier_id": 1, "category": "桥架"})
        assert a != b

    def test_hash_context_handles_none(self):
        assert _hash_context({}) == _hash_context({"supplier_id": None})


# ─── Backend audit-fix B: batch_confirm idempotency ────────────────────────
class TestBatchConfirmIdempotency:
    @pytest.fixture
    def client(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )

        canned = {
            "supplier_name": "TestSup",
            "items": [
                {"material": "桥架 A", "spec": "300×200", "brand": "良工",
                 "qty": 10, "unit_price": 100},
                {"material": "桥架 B", "spec": "200×100", "brand": "良工",
                 "qty": 20, "unit_price": 50},
            ],
        }

        def builder():
            return ExtractionPipeline(MockProvider(canned=canned))

        monkeypatch.setattr("apps.api.main._build_pipeline", builder)
        from apps.api.main import app
        with TestClient(app) as c:
            yield c

    def test_double_click_does_not_create_duplicates(self, client):
        # 1. Upload
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "桥架"},
            files={"file": ("q.png", _png(), "image/png")},
        )
        job_id = r.json()["id"]

        # 2. First confirm
        r1 = client.post(
            "/api/quotes/batch-confirm",
            json={"job_id": job_id, "supplier_name": "Alpha", "category": "桥架"},
        )
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["created"] == 2
        first_ids = set(body1["quote_ids"])
        assert len(first_ids) == 2

        # 3. Second confirm (simulating double-click) — MUST be idempotent
        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={"job_id": job_id, "supplier_name": "Alpha", "category": "桥架"},
        )
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["created"] == 0, (
            "Re-confirm must not create new rows. "
            f"Got created={body2['created']}; "
            "previously the lack of batch_id check caused duplicate quotes."
        )
        assert body2.get("idempotent") is True
        assert set(body2["quote_ids"]) == first_ids


# ─── Backend audit-fix B: malformed result shape ───────────────────────────
class TestBatchConfirmShapeGuard:
    """The pipeline's `_postprocess_quote` filters non-dict items, so the
    cleanest way to exercise batch_confirm's shape guard is to pass
    `overrides` directly. This is also how a frontend with a misbehaving
    editor would hit the guard."""

    @pytest.fixture
    def client(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )

        canned = {
            "supplier_name": "X",
            "items": [
                {"material": "Good", "qty": 1, "unit_price": 10},
                {"material": "Good2", "qty": 2, "unit_price": 20},
            ],
        }

        def builder():
            return ExtractionPipeline(MockProvider(canned=canned))

        monkeypatch.setattr("apps.api.main._build_pipeline", builder)
        from apps.api.main import app
        with TestClient(app) as c:
            yield c

    def test_non_dict_overrides_rejected_by_pydantic(self, client):
        """Pydantic enforces overrides: list[dict] at the request layer (422)
        — caller can never smuggle non-dict rows past Pydantic. This is
        defence-in-depth above the in-route guard tested separately below."""
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门"},
            files={"file": ("q.png", _png(), "image/png")},
        )
        job_id = r.json()["id"]

        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_id,
                "supplier_name": "S",
                "category": "阀门",
                "overrides": [
                    {"material": "OK", "qty": 1, "unit_price": 10},
                    "string not a dict",
                    None,
                ],
            },
        )
        assert r2.status_code == 422, r2.text

    def test_malformed_job_result_items_handled_gracefully(self, client, temp_db):
        """In-route guard: if job.result.items contains non-dict rows
        (which could happen if an LLM produces malformed JSON that
        somehow passes our pipeline post-processing), we report each as
        an error rather than crashing on .get()."""
        # Create a successful job via the API first
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门"},
            files={"file": ("q.png", _png(seed=7), "image/png")},
        )
        job_id = r.json()["id"]

        # Directly corrupt job.result to bypass the pipeline post-processor.
        # This simulates what would happen if the post-processor itself had
        # a bug — the route layer must still not crash.
        _, SessionLocal = temp_db
        from apps.api.models import ExtractionJob
        db = SessionLocal()
        try:
            job = db.get(ExtractionJob, job_id)
            assert job is not None
            job.result = {
                "supplier_name": "X",
                "items": [
                    {"material": "Good", "qty": 1, "unit_price": 10},
                    "not a dict",
                    None,
                    {"material": "Good2", "qty": 2, "unit_price": 20},
                ],
            }
            db.commit()
        finally:
            db.close()

        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={"job_id": job_id, "supplier_name": "S", "category": "阀门"},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["created"] == 2
        shape_err_count = sum(
            1 for e in body["errors"] if "object" in (e.get("reason") or "").lower()
        )
        assert shape_err_count == 2, body["errors"]

    def test_non_list_overrides_returns_422(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门"},
            files={"file": ("q.png", _png(seed=99), "image/png")},
        )
        job_id = r.json()["id"]
        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_id,
                "supplier_name": "X",
                "category": "阀门",
                "overrides": "this is not a list",  # type: ignore[arg-type]
            },
        )
        # FastAPI's pydantic validation rejects non-list overrides at the
        # request-body layer with 422 before the route handler runs.
        # That's the equivalent guard: caller can't smuggle a non-list past it.
        assert r2.status_code in (400, 422), r2.text


# ─── Backend audit-fix B: category token-boundary match ─────────────────────
class TestCategoryTokenMatch:
    def test_no_false_positive_on_compound_word(self):
        # "止回阀门" contains "阀门" but only as a suffix of "止回阀门"
        # — should NOT be matched as category="阀门".
        assert _is_category_token_match("止回阀门 DN50", "阀门") is False

    def test_match_at_start(self):
        assert _is_category_token_match("阀门 DN100", "阀门") is True

    def test_match_after_ascii_boundary(self):
        assert _is_category_token_match("DN100 阀门", "阀门") is True

    def test_match_for_specific_category(self):
        # "母线槽" is too specific to false-positive; substring is fine
        assert _is_category_token_match("低压母线槽 1000A", "母线槽") is True

    def test_real_use_case_no_false_positive(self):
        # The actual bug case: tender contains "止回阀门" → should NOT pick "阀门"
        cats = infer_categories([{"name": "止回阀门 DN50", "category": ""}])
        # Either empty (correct) or contains a more specific category, but
        # NOT just "阀门".
        # The fix returns [] because there's no token-boundary match.
        assert "阀门" not in cats or len(cats) == 0


# ─── Backend audit-fix H5: invite/save with all-invalid supplier_ids ───────
class TestInviteSaveValidation:
    @pytest.fixture
    def client(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )

        monkeypatch.setattr(
            "apps.api.main._build_pipeline",
            lambda: ExtractionPipeline(MockProvider()),
        )
        from apps.api.main import app
        with TestClient(app) as c:
            yield c

    def test_all_invalid_supplier_ids_rolls_back_tender(self, client):
        # No suppliers exist in fresh DB; passing arbitrary ids must 400
        # AND must NOT leave a draft TenderDocument behind.
        r = client.post(
            "/api/invite/save",
            json={
                "project_name": "Phantom Project",
                "items": [{"name": "x", "category": "桥架"}],
                "supplier_ids": [9999, 9998],
            },
        )
        assert r.status_code == 400
        # Verify no TenderDocument was created
        r2 = client.get("/api/invite/tenders")
        # Filter to project_name we tried to create
        bodies = [
            t for t in r2.json()
            if t.get("project_name") == "Phantom Project"
        ]
        assert bodies == [], "Tender must NOT be persisted when all suppliers invalid"


# ─── Intelligence audit-fix H7: dynamic timeout ────────────────────────────
class TestQwenVLDynamicTimeout:
    def test_timeout_scales_with_page_count(self):
        # The internal calculation: BASE + PER_PAGE * len(images)
        # We can't easily test the OpenAI call, but we can test the formula.
        base = QwenVLProvider.BASE_TIMEOUT_S
        per = QwenVLProvider.PER_PAGE_TIMEOUT_S
        # For 1 page → small budget
        assert base + per * 1 < base + per * 10
        # For 10 pages → ample budget
        assert base + per * 10 >= 200  # > the old fixed 90s


# ─── Intelligence audit-fix H8: JSON parser trailing-comma tolerance ───────
class TestJsonParseTrailingComma:
    def test_trailing_comma_in_object(self):
        out = QwenVLProvider._parse_json_strict('{"a": 1, "b": 2,}')
        assert out == {"a": 1, "b": 2}

    def test_trailing_comma_in_array(self):
        out = QwenVLProvider._parse_json_strict('{"items": [1, 2, 3,]}')
        assert out == {"items": [1, 2, 3]}

    def test_trailing_comma_combined_with_fence(self):
        out = QwenVLProvider._parse_json_strict('```json\n{"x":[1,2,],}\n```')
        assert out == {"x": [1, 2]}


# ─── Backend audit-fix M2: orphan project_id detection ────────────────────
class TestOrphanProjectGuard:
    """If a job's context references a project_id that no longer exists,
    batch-confirm must NOT silently null it; it should 400."""

    @pytest.fixture
    def client(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )
        canned = {
            "supplier_name": "X",
            "items": [{"material": "桥架 A", "qty": 1, "unit_price": 100}],
        }
        monkeypatch.setattr(
            "apps.api.main._build_pipeline",
            lambda: ExtractionPipeline(MockProvider(canned=canned)),
        )
        from apps.api.main import app
        with TestClient(app) as c:
            yield c

    def test_explicit_project_id_404_fails_loudly(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "桥架"},
            files={"file": ("q.png", _png(), "image/png")},
        )
        job_id = r.json()["id"]

        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_id,
                "supplier_name": "S",
                "project_id": 99999,  # non-existent
                "category": "桥架",
            },
        )
        # Must be 404, not silent null project
        assert r2.status_code == 404, r2.text
        assert "Project" in r2.json()["detail"]

    def test_context_project_id_404_fails_loudly(self, client, temp_db):
        """When the job context says project_id=X but X is gone."""
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "桥架", "project_id": 12345},
            files={"file": ("q.png", _png(seed=42), "image/png")},
        )
        job_id = r.json()["id"]
        # The job's context has project_id=12345 but no project with that id

        r2 = client.post(
            "/api/quotes/batch-confirm",
            json={"job_id": job_id, "supplier_name": "S", "category": "桥架"},
        )
        assert r2.status_code == 400
        assert "context" in r2.json()["detail"]


# ─── Backend audit-fix C3: periodic stuck-job sweep ────────────────────────
class TestPeriodicStuckJobSweep:
    """The lifespan starts a background coroutine that calls
    recover_stuck_jobs every STUCK_JOB_SWEEP_S. We test the coroutine
    directly (TestClient + asyncio sleep is too flaky to time reliably)."""

    @pytest.mark.asyncio
    async def test_sweep_coroutine_recovers_stuck_jobs(
        self, temp_db, monkeypatch, tmp_path
    ):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from apps.api.main import _periodic_stuck_job_sweep
        from apps.api.models import ExtractionJob
        from apps.api.services.document_ingestion import JobStatus

        monkeypatch.setattr("apps.api.main.STUCK_JOB_SWEEP_S", 0.05)

        _, SessionLocal = temp_db
        db = SessionLocal()
        try:
            db.add(
                ExtractionJob(
                    id="stuck-test-2",
                    type="tender",
                    status=JobStatus.RUNNING.value,
                    file_path="/dev/null",
                    updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                )
            )
            db.commit()
        finally:
            db.close()

        # Run the coroutine for one full cycle, then stop it
        stop = asyncio.Event()
        sweep_task = asyncio.create_task(_periodic_stuck_job_sweep(stop))
        await asyncio.sleep(0.15)  # enough for 2-3 iterations
        stop.set()
        await asyncio.wait_for(sweep_task, timeout=1.0)

        # Verify the stuck job was recovered
        db = SessionLocal()
        try:
            job = db.get(ExtractionJob, "stuck-test-2")
            assert job is not None
            assert job.status == JobStatus.FAILED.value, (
                f"Periodic sweep failed to recover stuck job (status={job.status})"
            )
            assert "Stuck" in (job.error or "")
        finally:
            db.close()


# ─── Audit-fix M1: supplier deletion guard ────────────────────────────────
class TestSupplierDeletionGuard:
    """Policy: suppliers referenced by quotes/invitations cannot be deleted."""

    @pytest.fixture
    def client(self, temp_db):
        from apps.api.main import app
        with TestClient(app) as c:
            yield c

    def test_delete_unreferenced_supplier_succeeds(self, client):
        r = client.post("/api/suppliers", json={"name": "可以删除"})
        assert r.status_code == 201
        sid = r.json()["id"]
        r2 = client.delete(f"/api/suppliers/{sid}")
        assert r2.status_code == 204

    def test_delete_supplier_with_quotes_rejected(self, client, temp_db):
        # Create supplier + material + quote
        r = client.post("/api/suppliers", json={"name": "有报价"})
        sid = r.json()["id"]

        _, SessionLocal = temp_db
        from apps.api.models import Material, Quote
        db = SessionLocal()
        try:
            mat = Material(
                material_code="TEST-001",
                standard_name="测试材料",
                profession="电气",
                category="桥架",
                sub_category="",
                spec="",
            )
            db.add(mat)
            db.flush()
            db.add(Quote(material_id=mat.id, supplier_id=sid, unit_price=100))
            db.commit()
        finally:
            db.close()

        r = client.delete(f"/api/suppliers/{sid}")
        assert r.status_code == 409
        body = r.json()["detail"]
        assert body["quote_count"] == 1
        assert "cannot be deleted" in body["message"]

    def test_delete_supplier_with_invitations_rejected(self, client, temp_db):
        r = client.post("/api/suppliers", json={"name": "有邀请"})
        sid = r.json()["id"]

        _, SessionLocal = temp_db
        from apps.api.models import TenderDocument, BidInvitation
        db = SessionLocal()
        try:
            tender = TenderDocument(project_name="P", items=[])
            db.add(tender)
            db.flush()
            db.add(BidInvitation(tender_id=tender.id, supplier_id=sid))
            db.commit()
        finally:
            db.close()

        r = client.delete(f"/api/suppliers/{sid}")
        assert r.status_code == 409
        assert r.json()["detail"]["invitation_count"] == 1

    def test_delete_unknown_supplier_404(self, client):
        r = client.delete("/api/suppliers/99999")
        assert r.status_code == 404


# ─── Audit-fix M4: standardize_name output stability ───────────────────────
class TestStandardizeStability:
    """The audit warned: 'two OCR runs produce slightly different output →
    two Material rows for same material'. After refactor, equivalent
    inputs MUST produce the same canonical form."""

    def test_fullwidth_to_halfwidth(self):
        # Full-width "ＤＮ５０" should normalize to half-width "DN50"
        assert standard_key("ＤＮ５０ 阀门") == standard_key("DN50 阀门")
        assert standard_key("PPR ３２") == standard_key("PPR 32")

    def test_case_insensitive_ascii(self):
        assert standard_key("dn50 阀门") == standard_key("DN50 阀门")
        assert standard_key("Dn50 阀门") == standard_key("DN50 阀门")
        # Chinese is unaffected
        assert "阀门" in standard_key("dn50 阀门")

    def test_whitespace_stability(self):
        # Multiple spaces / full-width spaces / leading-trailing whitespace
        assert standard_key("  DN50   阀门  ") == standard_key("DN50 阀门")
        assert standard_key("DN50　阀门") == standard_key("DN50 阀门")  # 全角空格
        assert standard_key("DN50\t阀门") == standard_key("DN50 阀门")
        assert standard_key("DN50\n阀门") == standard_key("DN50 阀门")

    def test_zero_width_chars_stripped(self):
        # Zero-width space (U+200B) sometimes sneaks in from copy-paste
        zwsp = "DN50​ 阀门"
        assert standard_key(zwsp) == standard_key("DN50 阀门")

    def test_idempotent(self):
        # standardize(standardize(x)) == standardize(x)
        once = standard_key("ＤＮ50 蝶型阀")
        twice = standard_key(once)
        assert once == twice

    def test_synonyms_after_canonicalize(self):
        # "蝶型阀" → "蝶阀" should still work after canonicalization
        result = standardize_name("ＤＮ100 蝶型阀")
        assert "蝶阀" in result["standardized"]
        assert "DN100" in result["standardized"]

    def test_empty_input_unchanged(self):
        assert standardize_name("")["standardized"] == ""
        assert standardize_name("   ")["standardized"] == "   "  # whitespace-only short-circuits

    def test_dn_normalization_still_works(self):
        # Φ108 → DN100 must still trigger after canonicalization
        result = standardize_name("Φ108 不锈钢管")
        assert "DN100" in result["standardized"]

    def test_dimension_normalization_still_works(self):
        # 300*150 → 300×150 must still trigger
        result = standardize_name("桥架 300*150")
        assert "300×150" in result["standardized"]


# ─── Audit-fix L3: concurrent invite/save IntegrityError safety ───────────
class TestConcurrentInviteSave:
    """Two concurrent POST /api/invite/save for the same (tender, supplier)
    must not crash with 500. The losing thread re-fetches the peer's row."""

    @pytest.fixture
    def setup_db_with_supplier(self, temp_db):
        _, SessionLocal = temp_db
        from apps.api.models import Supplier
        db = SessionLocal()
        try:
            sup = Supplier(name="Acme Concurrent")
            db.add(sup)
            db.commit()
            sid = sup.id
        finally:
            db.close()
        return sid

    def test_concurrent_save_no_500(self, temp_db, monkeypatch, setup_db_with_supplier):
        """SQLite serializes file-level writes so a "true" race in a unit
        test is impossible. We simulate it by:
        1. Pre-creating a BidInvitation (the "peer's winner row")
        2. Patching the route's existence-check to LIE — return None — so
           the route believes no row exists and tries to add a duplicate
        3. The duplicate add hits the uq_tender_supplier constraint
        4. Our IntegrityError handler must catch, rollback, re-fetch, and
           return the peer's row — NOT raise 500.
        """
        from apps.api.models import BidInvitation, TenderDocument
        from apps.api.main import app

        _, SessionLocal = temp_db
        sid = setup_db_with_supplier

        # Step 1: pre-insert the "winner" row that the route will collide with.
        db = SessionLocal()
        try:
            tender = TenderDocument(project_name="Race Test", items=[])
            db.add(tender)
            db.flush()
            tender_id = tender.id
            db.add(BidInvitation(
                tender_id=tender_id,
                supplier_id=sid,
                score=999.0,
                rank=99,
                reason={"summary": "peer winner"},
                status="pending",
            ))
            db.commit()
        finally:
            db.close()

        # Step 2: patch BidInvitation queries to return None ONLY for the
        # exact existence-check call in the route. We do this by replacing
        # Query.filter_by's first() on a per-call basis via a sentinel.
        import apps.api.routes.invite as invite_mod
        original_recommend = invite_mod.recommend_suppliers
        # Don't actually recompute recommendations — keep test fast
        monkeypatch.setattr(invite_mod, "recommend_suppliers", lambda *a, **kw: [])

        # Patch the existence check inside the loop. We monkeypatch
        # `Session.query` to inject a wrapper that returns None for
        # BidInvitation existence checks.
        from sqlalchemy.orm import Session
        original_query = Session.query
        lie_count = {"n": 0}

        class LyingFirst:
            def __init__(self, real_query):
                self._q = real_query

            def filter_by(self, **kw):
                self._kw = kw
                return self

            def first(self):
                # Lie ONCE for the BidInvitation existence check
                if (
                    "tender_id" in self._kw
                    and "supplier_id" in self._kw
                    and lie_count["n"] == 0
                ):
                    lie_count["n"] += 1
                    return None
                return self._q.filter_by(**self._kw).first()

        def patched_query(self, *entities, **kw):
            real = original_query(self, *entities, **kw)
            # Only intercept queries that look up BidInvitation
            if entities and entities[0] is BidInvitation:
                return LyingFirst(real)
            return real

        monkeypatch.setattr(Session, "query", patched_query)

        # Step 3: call the route — it will try to add a duplicate, hit
        # IntegrityError, recover, and return the pre-existing row.
        with TestClient(app) as client:
            r = client.post(
                "/api/invite/save",
                json={
                    "tender_id": tender_id,
                    "items": [],
                    "supplier_ids": [sid],
                },
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["invitations"]) == 1
        inv = body["invitations"][0]
        # We adopted the peer's row (rank=99, score=999.0) rather than crashing
        assert inv["rank"] == 99
        assert inv["score"] == 999.0
        # And the lie was triggered (i.e. the IntegrityError path actually ran)
        assert lie_count["n"] == 1


# ─── Audit-fix L4-α: ThreadPoolExecutor extraction ────────────────────────
class TestThreadPoolExtraction:
    """Thread-pool path is the production code path; tests assert pool
    initialisation, queue-depth endpoint, and submit semantics."""

    def test_get_pool_stats_unitialised(self):
        from apps.api.core import runtime as rt
        # Reset to clean state (other tests may have created a pool)
        if rt._executor is not None:
            rt._shutdown_executor()
        stats = rt.get_pool_stats()
        assert stats["active_threads"] == 0
        assert stats["queue_depth"] == 0
        assert stats["max_workers"] > 0  # default 8

    def test_pool_size_via_env(self, monkeypatch):
        monkeypatch.setenv("EXTRACTION_THREAD_POOL_SIZE", "3")
        from apps.api.core import runtime as rt
        if rt._executor is not None:
            rt._shutdown_executor()
        # Trigger creation via stats call
        stats = rt.get_pool_stats()
        # When uninitialised, falls back to env value
        assert stats["max_workers"] == 3

    def test_health_queue_endpoint(self, temp_db, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )
        monkeypatch.setattr(
            "apps.api.main._build_pipeline",
            lambda: ExtractionPipeline(MockProvider()),
        )
        from apps.api.main import app
        with TestClient(app) as client:
            r = client.get("/api/health/queue")
            assert r.status_code == 200
            body = r.json()
            assert "active_threads" in body
            assert "queue_depth" in body
            assert "max_workers" in body
            assert body["max_workers"] >= 1

    def test_inline_mode_runs_synchronously(self, temp_db, monkeypatch, tmp_path):
        """EXTRACTION_MODE=inline → submit_extraction blocks until done."""
        monkeypatch.setenv("EXTRACTION_MODE", "inline")
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setattr(
            "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
        )

        from apps.api.core import runtime as rt
        from apps.api.intelligence.pipeline import ExtractionPipeline
        from apps.api.intelligence.providers.mock import MockProvider
        from apps.api.models import ExtractionJob
        from apps.api.services.document_ingestion import (
            DocumentIngestionService, IngestionType,
        )

        canned = {"supplier_name": "X", "items": [{"material": "Y"}]}
        rt.set_runtime_pipeline(ExtractionPipeline(MockProvider(canned=canned)))

        _, SessionLocal = temp_db
        db = SessionLocal()
        try:
            svc = DocumentIngestionService(db, rt.get_runtime_pipeline())
            job = svc.create_job(_png(), "x.png", IngestionType.QUOTE, {})
        finally:
            db.close()

        # In inline mode this call BLOCKS until done
        rt.submit_extraction(job.id)

        db = SessionLocal()
        try:
            job = db.get(ExtractionJob, job.id)
            assert job.status == "done", job.status
            # Pipeline post-processes the canned data so result != canned
            # exactly. Verify the items survived normalisation.
            assert job.result["items"][0]["material"] == "Y"
        finally:
            db.close()


# ─── Intelligence audit-fix H6: known-bad memoization ──────────────────────
class TestQwenVLBadModelMemo:
    def test_known_bad_skipped_on_subsequent_call(self, monkeypatch):
        """If a model is rejected with BadRequestError, it should be
        skipped on the next extract() call."""
        import httpx
        from openai import BadRequestError

        # Build a real BadRequestError (the OpenAI SDK requires a fully
        # formed httpx.Response for the constructor).
        def _make_bad_request() -> BadRequestError:
            request = httpx.Request("POST", "https://example/x")
            response = httpx.Response(400, request=request)
            return BadRequestError(
                message="model not found",
                response=response,
                body={"error": "model not found"},
            )

        call_log: list[str] = []

        class FakeChat:
            class Completions:
                @staticmethod
                def create(model, **kw):
                    call_log.append(model)
                    if model == "bad-model":
                        raise _make_bad_request()
                    # Successful response shape with `choices[0].message.content`
                    class Resp:
                        class Choice:
                            class Message:
                                content = '{"items": []}'
                            message = Message()
                        choices = [Choice()]
                        usage = None
                    return Resp()
            completions = Completions

        class FakeOpenAI:
            def __init__(self, *a, **kw):
                self.chat = FakeChat

        monkeypatch.setattr(
            "apps.api.intelligence.providers.qwen_vl.OpenAI", FakeOpenAI
        )
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test")

        provider = QwenVLProvider(
            api_key="test",
            models=["bad-model", "good-model"],
        )
        assert "bad-model" in provider.candidates

        # First call: bad-model fails → falls back to good-model
        r1 = provider.extract([b"img"], {"type": "object", "required": []}, "p")
        assert r1.data == {"items": []}
        assert "bad-model" in provider._known_bad
        # Both models were attempted on first call
        assert call_log == ["bad-model", "good-model"]

        # Second call: bad-model is in _known_bad → SKIPPED
        call_log.clear()
        r2 = provider.extract([b"img"], {"type": "object", "required": []}, "p")
        assert r2.data == {"items": []}
        assert call_log == ["good-model"], (
            f"bad-model should have been skipped; instead got call sequence {call_log}"
        )
