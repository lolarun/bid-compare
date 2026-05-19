"""DocumentIngestionService — unified upload → recognise → structure pipeline.

Used by both flows:
- /api/intake/upload?type=tender  → invite flow
- /api/intake/upload?type=quote   → compare flow

The HTTP request returns immediately with a job_id; actual LLM call runs in
FastAPI BackgroundTasks. Frontend polls /api/intake/jobs/{id} every ~2s.

Design notes:
- Idempotency: SHA256(file_content) + type + business-context hash. Same
  content uploaded for different (supplier, project) returns DISTINCT jobs.
  This prevents cross-supplier cross-contamination when two suppliers
  legitimately submit the same blank template (or when the user reuploads).
- File stored to UPLOAD_DIR / {YYYYMMDD} / {hash}.{ext}; content is shared
  across jobs that have the same hash even if context differs.
- Stuck job recovery: on app startup, RUNNING jobs older than 5 min → FAILED
  (handled by main.lifespan). Background tasks always commit a terminal
  status even on exception, so the recovery path is the last line of defense.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from apps.api.intelligence.base import ProviderError
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.models import ExtractionJob

log = logging.getLogger(__name__)


# ─── Context-aware idempotency ────────────────────────────────────────────
# Only the keys that genuinely change the meaning of the extraction job.
# Other context (e.g. free-text annotations) is excluded so it doesn't
# multiply unrelated jobs.
_IDEMPOTENCY_CONTEXT_KEYS = ("supplier_id", "project_id", "category")


def _hash_context(context: dict[str, Any]) -> str:
    """Stable 16-char hash of the business-relevant context fields.

    Returns the literal string "noctx" when there's no relevant context, so
    two callers that pass {} and {"unused_key": ...} hash to the same bucket.
    """
    if not context:
        return "noctx"
    selected = {
        k: context.get(k)
        for k in _IDEMPOTENCY_CONTEXT_KEYS
        if context.get(k) not in (None, "")
    }
    if not selected:
        return "noctx"
    payload = json.dumps(selected, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _get_upload_dir() -> Path:
    """Resolve upload dir at call time so tests can monkeypatch UPLOAD_DIR."""
    return UPLOAD_DIR


UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))


class IngestionType(str, enum.Enum):
    TENDER = "tender"
    QUOTE = "quote"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


SUPPORTED_VISION_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class DocumentIngestionService:
    """Coordinates job creation, storage, async run, and status retrieval."""

    def __init__(self, db: Session, pipeline: ExtractionPipeline):
        self.db = db
        self.pipeline = pipeline

    # ─── public API ───────────────────────────────────────────────────────
    def create_job(
        self,
        file_content: bytes,
        filename: str,
        type: IngestionType | str,
        context: dict[str, Any] | None = None,
    ) -> ExtractionJob:
        """Create (or return existing idempotent) job. NOT yet executed.

        Idempotency key = (file_hash, type, context_hash). Same file uploaded
        for two different suppliers gets two distinct jobs.
        """
        type_str = type.value if isinstance(type, IngestionType) else str(type)
        if type_str not in {t.value for t in IngestionType}:
            raise ValueError(f"Unknown ingestion type: {type_str}")
        if not filename:
            raise ValueError("filename is required")

        ctx = context or {}
        file_hash = hashlib.sha256(file_content).hexdigest()
        ctx_hash = _hash_context(ctx)

        # Idempotency: same content + same type + same business context →
        # return the latest non-failed job (caller can retry by waiting for a
        # FAILED job to age out, which produces a fresh job below).
        existing = (
            self.db.query(ExtractionJob)
            .filter(
                ExtractionJob.file_hash == file_hash,
                ExtractionJob.type == type_str,
            )
            .order_by(ExtractionJob.created_at.desc())
            .all()
        )
        for prior in existing:
            if prior.status == JobStatus.FAILED.value:
                continue
            if _hash_context(prior.context or {}) == ctx_hash:
                log.info("Idempotent hit: returning existing job %s", prior.id)
                return prior

        # Persist file (shared by all jobs with same content hash)
        ext = Path(filename).suffix.lower() or ".bin"
        if ext not in SUPPORTED_VISION_EXT and ext not in {".xlsx", ".xls", ".csv"}:
            raise ValueError(f"Unsupported file extension: {ext}")
        date_dir = datetime.now(timezone.utc).strftime("%Y%m%d")
        save_dir = _get_upload_dir() / date_dir
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"{file_hash}{ext}"
        if not save_path.exists():
            save_path.write_bytes(file_content)

        job = ExtractionJob(
            id=uuid.uuid4().hex,
            type=type_str,
            status=JobStatus.PENDING.value,
            filename=filename,
            file_hash=file_hash,
            file_size=len(file_content),
            file_path=str(save_path),
            mime_type=mimetypes.guess_type(filename)[0] or "",
            context=ctx,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def run_job(self, job_id: str) -> None:
        """Synchronously execute extraction for a single job.

        Called by FastAPI BackgroundTasks. Uses a fresh DB session so it
        survives the request context shutting down. Always commits a
        terminal status (DONE/FAILED) so the stuck-job recovery path is
        only the last line of defense.
        """
        from apps.api.core.database import SessionLocal

        db = SessionLocal()
        job: ExtractionJob | None = None
        try:
            job = db.get(ExtractionJob, job_id)
            if not job:
                log.error("run_job: job %s not found", job_id)
                return
            job.status = JobStatus.RUNNING.value
            db.commit()

            try:
                ext = Path(job.file_path).suffix.lower()
                if ext in {".xlsx", ".xls", ".csv"}:
                    # Excel/CSV path bypasses the LLM and uses import_service.
                    # For Phase 0 we just stub; full wiring happens in Phase 3.
                    result = {
                        "items": [],
                        "note": "Excel/CSV ingestion uses import_service (Phase 3)",
                    }
                elif job.type == IngestionType.TENDER.value:
                    resp = self.pipeline.extract_tender(job.file_path)
                    result = resp.data
                    job.tokens_used = resp.tokens_used
                    job.duration_ms = resp.duration_ms
                    job.provider = resp.provider
                    job.confidence = resp.confidence
                elif job.type == IngestionType.QUOTE.value:
                    resp = self.pipeline.extract_quote(job.file_path, job.context or {})
                    result = resp.data
                    job.tokens_used = resp.tokens_used
                    job.duration_ms = resp.duration_ms
                    job.provider = resp.provider
                    job.confidence = resp.confidence
                else:
                    raise ValueError(f"Unknown job type: {job.type}")

                job.result = result
                job.status = JobStatus.DONE.value
                job.error = ""
                db.commit()
                log.info("Job %s done (%d items)", job.id, len(result.get("items") or []))
            except Exception as e:
                # Catch-all: any failure flips job → FAILED.
                # `except` must not propagate; lifespan recovery is a fallback only.
                log.exception("Job %s failed", job.id)
                try:
                    db.rollback()
                    # Re-fetch job in case the rollback discarded mutations
                    job = db.get(ExtractionJob, job_id)
                    if job:
                        job.status = JobStatus.FAILED.value
                        job.error = f"{type(e).__name__}: {e}"[:1000]
                        db.commit()
                except Exception:
                    log.exception("Failed to mark job %s as FAILED", job_id)
                    db.rollback()
        finally:
            db.close()

    def get_job(self, job_id: str) -> ExtractionJob | None:
        return self.db.get(ExtractionJob, job_id)

    def list_jobs(
        self,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[ExtractionJob]:
        q = self.db.query(ExtractionJob)
        if type:
            q = q.filter(ExtractionJob.type == type)
        if status:
            q = q.filter(ExtractionJob.status == status)
        return q.order_by(ExtractionJob.created_at.desc()).limit(limit).all()

    # ─── housekeeping ─────────────────────────────────────────────────────
    @staticmethod
    def recover_stuck_jobs(db: Session, max_age_minutes: int = 5) -> int:
        """Mark RUNNING jobs older than max_age as FAILED. Call at app startup."""
        threshold = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        stuck = (
            db.query(ExtractionJob)
            .filter(
                ExtractionJob.status == JobStatus.RUNNING.value,
                ExtractionJob.updated_at < threshold,
            )
            .all()
        )
        for j in stuck:
            j.status = JobStatus.FAILED.value
            j.error = "Stuck in RUNNING beyond threshold; recovered at startup."
        if stuck:
            db.commit()
        return len(stuck)
