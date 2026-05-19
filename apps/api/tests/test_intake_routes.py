"""Integration tests for /api/intake/* via TestClient.

Uses MockProvider so no LLM is called. Verifies:
- POST upload returns job_id and runs background extraction
- GET jobs/{id} reflects status transition
- GET jobs lists recent jobs
"""

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def client(temp_db, monkeypatch, fixture_dir, tmp_path):
    """Build app with MockProvider + isolated DB + tmp uploads dir."""
    # Force mock provider regardless of env
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )
    # Patch the pipeline-builder so it picks our fixture-loading MockProvider
    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider

    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(MockProvider(fixture_dir=fixture_dir)),
    )

    # Import app AFTER patches
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


class TestUpload:
    def test_upload_tender_returns_job(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("t.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "tender"
        assert body["status"] in {"pending", "running", "done"}
        assert body["id"]

    def test_upload_quote_with_context(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "quote", "supplier_id": 5, "project_id": 12, "category": "阀门"},
            files={"file": ("q.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["context"] == {"supplier_id": 5, "project_id": 12, "category": "阀门"}

    def test_upload_invalid_type(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "garbage"},
            files={"file": ("x.png", _png(), "image/png")},
        )
        assert r.status_code == 400

    def test_upload_empty_file(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("empty.png", b"", "image/png")},
        )
        assert r.status_code == 400


class TestJobLifecycle:
    def test_polling_eventually_done(self, client):
        r = client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("t.png", _png(), "image/png")},
        )
        job_id = r.json()["id"]

        # BackgroundTasks runs synchronously in TestClient — should be DONE immediately
        # after the request returns (TestClient awaits background tasks).
        r2 = client.get(f"/api/intake/jobs/{job_id}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "done", body
        assert body["result"]["project_name"].startswith("测试招标")

    def test_get_unknown(self, client):
        r = client.get("/api/intake/jobs/does-not-exist")
        assert r.status_code == 404

    def test_list(self, client):
        # Upload a couple
        for i in range(2):
            buf = io.BytesIO()
            # vary contents so hashes differ
            Image.new("RGB", (16, 16), (i, i, i)).save(buf, format="PNG")
            client.post(
                "/api/intake/upload",
                data={"type": "tender"},
                files={"file": (f"t{i}.png", buf.getvalue(), "image/png")},
            )
        r = client.get("/api/intake/jobs", params={"type": "tender", "limit": 10})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 2
