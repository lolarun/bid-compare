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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def _merge_quality_metadata(data: dict, metadata: dict) -> dict:
    """把 ExtractionResponse.metadata 里的质量/台账/方向信号并入 job.result。

    评审 R2：此前这里只取 `resp.data`，`resp.metadata` 里的 quality_status/
    quality_blocking_reasons/row_ledger/page_count/target_pages/rotations/
    orientation_unresolved 全部被丢弃——不是"前端没接"，是这些字段从未到达
    过 job.result，API 层拿不到，前端不可能显示。这里改为整体保留（除
    doc_meta，它已单独落 `_doc_meta`，避免与 quote_confirmation_service 的
    checksum 门读取路径重复/冲突），前缀 `_quality`，与既有 `_doc_meta`/
    `_checksum` 的命名约定一致（下划线前缀 = 元数据，不是识别出的报价字段，
    不会被误当成一行数据渲染）。`JobResponse.result` 是裸 dict，新增键无需
    改 schema 即可透传到前端。
    """
    quality_meta = {k: v for k, v in metadata.items() if k != "doc_meta"}
    if not quality_meta:
        return data
    merged = dict(data)
    merged["_quality"] = quality_meta
    if metadata.get("doc_meta"):
        merged["_doc_meta"] = metadata["doc_meta"]
    return merged


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
        force: bool = False,
    ) -> ExtractionJob:
        """Create (or return existing idempotent) job. NOT yet executed.

        Idempotency key = (file_hash, type, context_hash). Same file uploaded
        for two different suppliers gets two distinct jobs.

        `force=True` **跳过幂等命中，强制新建一个 job 重新识别**。

        为什么需要它（2026-08-25）：幂等键里**没有代码版本**，所以识别逻辑改好
        之后，同一份文件在同一项目下重传会命中旧 job、直接返回**改动前的旧结果**。
        这个坑本轮咬过两次——项目 106 的品类，以及三份报价单"明细合计 ¥0"。
        用户此前唯一的出路是新建项目（换掉 context_hash），既不直观也留一堆废项目。

        **默认仍然幂等**：不传 `force` 时行为逐字节不变，重复上传照样省掉一次
        识别调用（那是真实的钱）。强制重跑必须是用户显式点出来的动作。
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
                if force:
                    log.info("force=True，跳过幂等命中（旧 job %s），重新识别", prior.id)
                    break
                log.info("Idempotent hit: returning existing job %s", prior.id)
                return prior

        # Persist file (shared by all jobs with same content hash)
        ext = Path(filename).suffix.lower() or ".bin"
        if ext not in SUPPORTED_VISION_EXT and ext not in {".xlsx", ".xls", ".csv"}:
            raise ValueError(f"Unsupported file extension: {ext}")
        date_dir = datetime.now(UTC).strftime("%Y%m%d")
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

            def update_progress(
                stage: str, pct: int, *,
                stage_current: int | None = None, stage_total: int | None = None,
            ) -> None:
                current = db.get(ExtractionJob, job_id)
                if not current or current.status != JobStatus.RUNNING.value:
                    return
                current.progress_stage = stage
                current.progress_pct = max(current.progress_pct or 0, pct)
                # design/24 B2：阶段内进度——总是按本次调用的值原样写（含 None），
                # 不做"没传就保留上次的值"。理由：换阶段时如果沿用上一阶段的
                # current/total，会有一小段时间显示"识别报价清单"配着上一阶段
                # 剩下的"8/8"，误导用户以为这个新阶段也快完成了。
                current.stage_current = stage_current
                current.stage_total = stage_total
                db.commit()

            try:
                ext = Path(job.file_path).suffix.lower()
                if ext in {".xlsx", ".xls", ".csv"} and job.type == IngestionType.QUOTE.value:
                    # Tabular bypass: deterministic pandas extraction.
                    # Skips OCR/LLM; produces the same result shape as _postprocess_quote
                    # so that batch-confirm / anchor-match / 90-row matrix work unchanged.
                    from apps.api.services.ingestion.tabular_ingestion import (
                        extract_quote_tabular,
                    )
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
                    result = _merge_quality_metadata(resp.data, resp.metadata)
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
                    result = _merge_quality_metadata(resp.data, resp.metadata)
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
        threshold = datetime.now(UTC) - timedelta(minutes=max_age_minutes)
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
