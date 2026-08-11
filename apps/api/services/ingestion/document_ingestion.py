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

from sqlalchemy import select
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
    TENDER_BIDLIST = "tender_bidlist"  # 招标文件 PDF → 投标清单锚点 + 品牌映射


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
        existing = self.db.scalars(
            select(ExtractionJob).where(
                ExtractionJob.file_hash == file_hash,
                ExtractionJob.type == type_str,
            )
            .order_by(ExtractionJob.created_at.desc())
        ).all()
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
            progress_stage="已接收文件",
            progress_pct=0,
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
            job.progress_stage = "准备识别"
            job.progress_pct = 5
            db.commit()

            def update_progress(stage: str, pct: int) -> None:
                current = db.get(ExtractionJob, job_id)
                if not current or current.status != JobStatus.RUNNING.value:
                    return
                current.progress_stage = stage
                current.progress_pct = max(current.progress_pct or 0, pct)
                db.commit()

            try:
                ext = Path(job.file_path).suffix.lower()
                if ext in {".xlsx", ".xls", ".csv"} and job.type == IngestionType.QUOTE.value:
                    # Tabular bypass: deterministic pandas extraction.
                    # Skips OCR/LLM; produces the same result shape as _postprocess_quote
                    # so that batch-confirm / anchor-match / 90-row matrix work unchanged.
                    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular
                    result = extract_quote_tabular(job.file_path, job.context or {})
                    update_progress("确定性解析完成", 90)
                elif job.type == IngestionType.TENDER_BIDLIST.value:
                    # 招标文件 PDF → 投标清单锚点 + 品牌映射（返回 dict，自带 items/brand_*）
                    ctx = job.context or {}
                    result = self.pipeline.extract_tender_bidlist(
                        job.file_path,
                        progress_cb=update_progress,
                        bidlist_pages=ctx.get("bidlist_pages"),
                        brand_page=ctx.get("brand_page"),
                    )
                elif job.type == IngestionType.TENDER.value:
                    resp = self.pipeline.extract_tender(
                        job.file_path,
                        progress_cb=update_progress,
                    )
                    result = resp.data
                    job.tokens_used = resp.tokens_used
                    job.duration_ms = resp.duration_ms
                    job.provider = resp.provider
                    job.confidence = resp.confidence
                elif job.type == IngestionType.QUOTE.value:
                    resp = self.pipeline.extract_quote(
                        job.file_path,
                        job.context or {},
                        progress_cb=update_progress,
                    )
                    result = resp.data
                    if resp.metadata.get("doc_meta"):
                        result = dict(result)
                        result["_doc_meta"] = resp.metadata["doc_meta"]
                    job.tokens_used = resp.tokens_used
                    job.duration_ms = resp.duration_ms
                    job.provider = resp.provider
                    job.confidence = resp.confidence
                else:
                    raise ValueError(f"Unknown job type: {job.type}")

                job.result = result
                job.status = JobStatus.DONE.value
                job.error = ""
                job.progress_stage = "已识别"
                job.progress_pct = 100
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
                        job.progress_stage = "识别失败"
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
        stmt = select(ExtractionJob)
        if type:
            stmt = stmt.where(ExtractionJob.type == type)
        if status:
            stmt = stmt.where(ExtractionJob.status == status)
        return self.db.scalars(stmt.order_by(ExtractionJob.created_at.desc()).limit(limit)).all()

    # ─── housekeeping ─────────────────────────────────────────────────────
    @staticmethod
    def recover_stuck_jobs(
        db: Session,
        max_age_minutes: int = 5,
        *,
        include_pending: bool = False,
    ) -> int:
        """Mark stuck jobs as FAILED. Call at app startup and on a periodic sweep.

        识别在进程内以后台任务执行，无法跨进程重启续跑。因此：
        - 启动时（max_age_minutes=0, include_pending=True）：任何仍为 RUNNING/PENDING
          的 job 必然是上次进程崩溃/重启遗留的孤儿，立即标 FAILED。否则上传幂等会把这条
          孤儿（非 failed）原样返回，用户重传也拿不到新识别（死循环卡在 20%）。
        - 周期清扫（默认仅 RUNNING + 年龄阈值）：清理运行中卡死的 job，不误伤刚入队的。
        """
        threshold = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        statuses = [JobStatus.RUNNING.value]
        if include_pending:
            statuses.append(JobStatus.PENDING.value)
        stuck = db.scalars(
            select(ExtractionJob).where(
                ExtractionJob.status.in_(statuses),
                ExtractionJob.updated_at < threshold,
            )
        ).all()
        for j in stuck:
            j.status = JobStatus.FAILED.value
            j.error = (
                "孤儿任务：进程崩溃/重启导致后台识别中断，已在启动时回收（重传可重新识别）。"
            )
            j.progress_stage = "识别中断"
        if stuck:
            db.commit()
        return len(stuck)
