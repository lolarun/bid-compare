"""Unit tests for DocumentIngestionService — job lifecycle + idempotency."""

import io
import os
from unittest.mock import patch

import pytest

from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA
from apps.api.intelligence.prompts import TENDER_PROMPT
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.models import ExtractionJob
from apps.api.services.document_ingestion import (
    DocumentIngestionService,
    IngestionType,
    JobStatus,
)


def _png_bytes() -> bytes:
    """Generate a minimal valid PNG (1×1 white pixel) for tests."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def pipeline(fixture_dir):
    # Use a MockProvider that loads our tender/quote fixtures
    return ExtractionPipeline(MockProvider(fixture_dir=fixture_dir))


@pytest.fixture
def service(db_session, pipeline, tmp_path, monkeypatch):
    # Redirect uploads to tmp_path so we don't pollute repo
    monkeypatch.setattr(
        "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )
    return DocumentIngestionService(db_session, pipeline)


class TestCreateJob:
    def test_create_pending(self, service):
        content = _png_bytes()
        job = service.create_job(
            content, "test.png", IngestionType.QUOTE, {"supplier_id": 1}
        )
        assert job.id
        assert job.status == JobStatus.PENDING.value
        assert job.type == IngestionType.QUOTE.value
        assert job.file_size == len(content)
        assert job.file_hash  # 64 hex chars
        assert len(job.file_hash) == 64
        assert os.path.exists(job.file_path)
        assert job.context == {"supplier_id": 1}

    def test_idempotent_same_hash_returns_existing(self, service):
        c = _png_bytes()
        j1 = service.create_job(c, "a.png", IngestionType.TENDER)
        j2 = service.create_job(c, "b.png", IngestionType.TENDER)  # different name, same content
        assert j1.id == j2.id

    def test_different_types_get_different_jobs(self, service):
        c = _png_bytes()
        j1 = service.create_job(c, "x.png", IngestionType.TENDER)
        j2 = service.create_job(c, "x.png", IngestionType.QUOTE)
        assert j1.id != j2.id

    def test_failed_job_allows_retry(self, service, db_session):
        c = _png_bytes()
        j1 = service.create_job(c, "x.png", IngestionType.TENDER)
        j1.status = JobStatus.FAILED.value
        db_session.commit()
        j2 = service.create_job(c, "x.png", IngestionType.TENDER)
        assert j2.id != j1.id  # new job allowed after failure

    def test_unsupported_extension(self, service):
        with pytest.raises(ValueError):
            service.create_job(b"data", "spec.docx", IngestionType.QUOTE)

    def test_invalid_type(self, service):
        with pytest.raises(ValueError):
            service.create_job(_png_bytes(), "x.png", "bogus")


class TestRunJob:
    def test_quote_run_marks_done(self, service, db_session):
        c = _png_bytes()
        job = service.create_job(c, "q.png", IngestionType.QUOTE)
        service.run_job(job.id)
        db_session.refresh(job)
        assert job.status == JobStatus.DONE.value
        # The MockProvider loads fixtures/quote.json
        assert job.result is not None
        assert "items" in job.result
        assert len(job.result["items"]) == 2

    def test_tender_run_marks_done(self, service, db_session):
        c = _png_bytes()
        job = service.create_job(c, "t.png", IngestionType.TENDER)
        service.run_job(job.id)
        db_session.refresh(job)
        assert job.status == JobStatus.DONE.value
        assert job.result["project_name"].startswith("测试招标")
        assert len(job.result["items"]) == 3


class TestStuckJobRecovery:
    def test_recovers_old_running_jobs(self, db_session):
        from datetime import datetime, timezone, timedelta

        old = ExtractionJob(
            id="old-1",
            type="tender",
            status=JobStatus.RUNNING.value,
            file_path="/dev/null",
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        fresh = ExtractionJob(
            id="fresh-1",
            type="tender",
            status=JobStatus.RUNNING.value,
            file_path="/dev/null",
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add_all([old, fresh])
        db_session.commit()

        recovered = DocumentIngestionService.recover_stuck_jobs(db_session, 5)
        db_session.refresh(old)
        db_session.refresh(fresh)
        assert recovered == 1
        assert old.status == JobStatus.FAILED.value
        assert fresh.status == JobStatus.RUNNING.value
