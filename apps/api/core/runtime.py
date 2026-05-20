"""Runtime singletons accessible from outside the FastAPI request context.

Two things live here:

1. The global `ExtractionPipeline` instance, so background tasks (running
   outside the request scope) can access it without a `Request` object.

2. A process-wide `ThreadPoolExecutor` for extraction work.

   ── Why a thread pool, not BackgroundTasks ──
   FastAPI's BackgroundTasks runs work AFTER the response is sent, but in the
   same event loop. A 30 s Qwen-VL call would block that uvicorn worker from
   accepting any other request for 30 s.

   Qwen-VL extraction is IO-bound (it spends nearly all wall-clock time
   waiting on DashScope's HTTP response), so a Python thread pool achieves
   true parallelism despite the GIL — threads release it on socket reads.

   With `uvicorn --workers 4` + `EXTRACTION_THREAD_POOL_SIZE=8`, the process
   can handle up to 4×8 = 32 concurrent OCR jobs while still serving the
   HTTP API on the main async loop.

   The pool is shut down gracefully at app teardown.

Keep this module side-effect-free at import time. Don't import models or
services here — only resolve them when called.
"""

from __future__ import annotations

import atexit
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid runtime import cycle
    from apps.api.intelligence.pipeline import ExtractionPipeline

log = logging.getLogger(__name__)

_pipeline: "Optional[ExtractionPipeline]" = None
_executor: Optional[ThreadPoolExecutor] = None


def _pool_size() -> int:
    """Threads per uvicorn worker. Default 8; override via env."""
    try:
        return max(1, int(os.getenv("EXTRACTION_THREAD_POOL_SIZE", "8")))
    except ValueError:
        return 8


def set_runtime_pipeline(pipeline: "ExtractionPipeline | None") -> None:
    global _pipeline
    _pipeline = pipeline


def get_runtime_pipeline() -> "Optional[ExtractionPipeline]":
    return _pipeline


def get_executor() -> ThreadPoolExecutor:
    """Lazy singleton — created on first call within this process.

    Each uvicorn worker is a separate process; each gets its own pool.
    """
    global _executor
    if _executor is None:
        size = _pool_size()
        _executor = ThreadPoolExecutor(
            max_workers=size, thread_name_prefix="extract"
        )
        log.info("ExtractionThreadPool initialised (max_workers=%d)", size)
        atexit.register(_shutdown_executor)
    return _executor


def submit_extraction(job_id: str) -> None:
    """Queue an extraction job for background processing.

    Honours `EXTRACTION_MODE=inline` to run synchronously in the caller's
    thread — used by tests with TestClient where polling for completion
    would slow the suite down. Default `EXTRACTION_MODE=thread` (or unset)
    submits to the ThreadPoolExecutor.

    Imports are deferred to keep this module side-effect-free at boot.
    """
    from apps.api.core.database import SessionLocal
    from apps.api.services.document_ingestion import DocumentIngestionService

    pipeline = get_runtime_pipeline()
    if pipeline is None:
        log.error("submit_extraction(%s): pipeline not initialised", job_id)
        return

    def _run() -> None:
        db = SessionLocal()
        try:
            DocumentIngestionService(db, pipeline).run_job(job_id)
        except Exception:
            log.exception("Extraction worker failed for job %s", job_id)
        finally:
            db.close()

    if os.getenv("EXTRACTION_MODE", "thread").lower() == "inline":
        _run()
    else:
        get_executor().submit(_run)


def get_pool_stats() -> dict[str, int]:
    """Snapshot of executor state for /api/health/queue.

    Returns:
        active_threads: threads currently busy
        queue_depth: tasks waiting in the work queue (not yet started)
        max_workers: configured pool size
    """
    if _executor is None:
        return {"active_threads": 0, "queue_depth": 0, "max_workers": _pool_size()}
    # ThreadPoolExecutor.{_threads, _work_queue} are documented private but
    # stable since 3.7 — used the same way by many production codebases.
    # `_threads` includes both busy and idle threads; we can't cheaply tell
    # which are busy without a custom wrapper, so report total instead.
    return {
        "active_threads": len(_executor._threads),  # type: ignore[attr-defined]
        "queue_depth": _executor._work_queue.qsize(),  # type: ignore[attr-defined]
        "max_workers": _executor._max_workers,
    }


def _shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        log.info("ExtractionThreadPool shutting down")
        _executor.shutdown(wait=True, cancel_futures=False)
        _executor = None


def shutdown_runtime() -> None:
    """Called from lifespan teardown."""
    _shutdown_executor()
    set_runtime_pipeline(None)
