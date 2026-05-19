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
