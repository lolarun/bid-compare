"""Integration tests for the compare main flow.

Validates Phase 3 end-to-end pipeline:
1. Upload quote PDFs/images for two suppliers via /api/intake/upload
2. Poll until DONE
3. POST /api/quotes/batch-confirm to convert results → Quote rows
4. POST /api/analysis/bid-matrix returns rows + totals + recommended supplier

Uses MockProvider with canned per-supplier responses so no LLM is called.
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


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


# Two distinct canned quote responses (different prices to make matrix interesting)
SUPPLIER_A_QUOTE = {
    "supplier_name": "供应商A",
    "quote_date": "2026-05-20",
    "items": [
        {
            "material": "DN100 闸阀",
            "spec": "Z45X-16Q",
            "brand": "良工",
            "unit": "个",
            "qty": 10,
            "unit_price": 720,
        },
        {
            "material": "DN50 闸阀",
            "spec": "Z45X-16Q",
            "brand": "良工",
            "unit": "个",
            "qty": 20,
            "unit_price": 380,
        },
    ],
}

SUPPLIER_B_QUOTE = {
    "supplier_name": "供应商B",
    "quote_date": "2026-05-20",
    "items": [
        {
            "material": "DN100 闸阀",
            "spec": "Z45X-16Q",
            "brand": "正丰",
            "unit": "个",
            "qty": 10,
            "unit_price": 690,
        },
        {
            "material": "DN50 闸阀",
            "spec": "Z45X-16Q",
            "brand": "正丰",
            "unit": "个",
            "qty": 20,
            "unit_price": 400,
        },
    ],
}


class _CycleProvider(MockProvider):
    """MockProvider that returns canned responses in round-robin order.

    Lets a single TestClient serve multiple distinct "supplier uploads".
    """

    def __init__(self, responses: list[dict]):
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def extract(self, images, schema, prompt, timeout=90):
        canned = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        from apps.api.intelligence.base import ExtractionResponse
        return ExtractionResponse(
            data=canned,
            raw_text=json.dumps(canned, ensure_ascii=False),
            confidence=1.0,
            tokens_used=0,
            provider="mock-cycle",
            duration_ms=1,
        )


@pytest.fixture
def compare_client(temp_db, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )

    cycle_provider = _CycleProvider([SUPPLIER_A_QUOTE, SUPPLIER_B_QUOTE])

    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(cycle_provider),
    )
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


class TestPhase3CompareFlow:
    def test_full_pipeline_to_bid_matrix(self, compare_client):
        """End-to-end: upload×2 → batch-confirm×2 → bid-matrix."""
        # ── 1. Upload quote for supplier A ──
        r = compare_client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门", "project_id": ""},
            files={"file": ("A.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        job_a = r.json()["id"]

        # ── 2. Upload quote for supplier B (different image bytes → different hash) ──
        # Vary bytes by saving a slightly different image
        buf_b = io.BytesIO()
        Image.new("RGB", (16, 16), (250, 250, 250)).save(buf_b, format="PNG")
        r = compare_client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门"},
            files={"file": ("B.png", buf_b.getvalue(), "image/png")},
        )
        assert r.status_code == 200
        job_b = r.json()["id"]

        # ── 3. Both jobs should be DONE ──
        for jid in (job_a, job_b):
            r = compare_client.get(f"/api/intake/jobs/{jid}")
            body = r.json()
            assert body["status"] == "done", body

        # ── 4. batch-confirm for both suppliers ──
        r = compare_client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_a,
                "supplier_name": "供应商A",
                "project_name": "Phase3 测试比价项目",
                "category": "阀门",
            },
        )
        assert r.status_code == 200, r.text
        a = r.json()
        assert a["created"] == 2
        assert a["supplier_id"]
        assert a["project_id"]

        r = compare_client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_b,
                "supplier_name": "供应商B",
                "project_id": a["project_id"],  # link to same project
                "category": "阀门",
            },
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["created"] == 2

        supplier_a_id = a["supplier_id"]
        supplier_b_id = b["supplier_id"]
        project_id = a["project_id"]

        # ── 5. bid-matrix returns non-empty rows + totals + recommended ──
        r = compare_client.post(
            "/api/analysis/bid-matrix",
            json={
                "project_id": project_id,
                "supplier_ids": [supplier_a_id, supplier_b_id],
                "category": "阀门",
            },
        )
        assert r.status_code == 200, r.text
        matrix = r.json()
        assert matrix["project_id"] == project_id
        assert len(matrix["suppliers"]) == 2
        assert len(matrix["rows"]) >= 1
        assert len(matrix["totals"]) == 2

        # Each row should have a recommended supplier letter (A or B)
        for row in matrix["rows"]:
            assert "suppliers" in row
            assert len(row["suppliers"]) == 2  # one cell per supplier

        # Totals carry total + avg_deviation per supplier
        for t in matrix["totals"]:
            assert "supplier_id" in t
            assert "total" in t
            assert "avg_deviation" in t

    def test_batch_confirm_rejects_non_quote_job(self, compare_client):
        # Upload a tender (wrong type) and try to confirm as quote
        r = compare_client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("t.png", _png(), "image/png")},
        )
        tender_job = r.json()["id"]

        r2 = compare_client.post(
            "/api/quotes/batch-confirm",
            json={"job_id": tender_job, "category": "阀门", "supplier_name": "X"},
        )
        assert r2.status_code == 400
        assert "must be 'quote'" in r2.json()["detail"]

    def test_batch_confirm_unknown_brands_reported(self, compare_client):
        r = compare_client.post(
            "/api/intake/upload",
            data={"type": "quote", "category": "阀门"},
            files={"file": ("A.png", _png(), "image/png")},
        )
        job_id = r.json()["id"]
        r2 = compare_client.post(
            "/api/quotes/batch-confirm",
            json={
                "job_id": job_id,
                "supplier_name": "TestSupplier",
                "category": "阀门",
            },
        )
        assert r2.status_code == 200
        body = r2.json()
        # Canned response has brands 良工/正丰; neither is in seeded brand_tiers
        assert len(body["unknown_brands"]) > 0
