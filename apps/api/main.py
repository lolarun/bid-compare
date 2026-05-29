"""FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.core.config import get_settings
from apps.api.core.database import init_db, SessionLocal
from apps.api.core.runtime import (
    get_pool_stats,
    set_runtime_pipeline,
    shutdown_runtime,
)
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
from apps.api.intelligence.base import ProviderError
from apps.api.routes import all_routers
from apps.api.routes.auth import router as auth_router, get_current_user
from apps.api.services.document_ingestion import DocumentIngestionService

log = logging.getLogger("mempas")

# AUDIT-FIX C3: periodic stuck-job recovery beyond the startup pass.
# Background task runs every STUCK_JOB_SWEEP_S and flips any RUNNING job
# whose updated_at is older than the recovery threshold to FAILED.
STUCK_JOB_SWEEP_S = 60
STUCK_JOB_MAX_AGE_MINUTES = 30

STATIC_DIR = Path(__file__).resolve().parent.parent / "www" / "dist"


def _build_pipeline() -> ExtractionPipeline:
    """Choose provider per LLM_PROVIDER setting; fall back gracefully.

      dashscope_ocr (default) → DashScopeOCRProvider (two-stage: OCR + text LLM)
      mock                    → MockProvider
    """
    settings = get_settings()
    requested = (settings.LLM_PROVIDER or "dashscope_ocr").lower()

    if requested == "mock":
        log.info("LLM_PROVIDER=mock → using MockProvider")
        return ExtractionPipeline(MockProvider())

    # Default: dashscope_ocr (two-stage OCR + LLM)
    if not settings.DASHSCOPE_API_KEY:
        log.warning("DASHSCOPE_API_KEY not set; using MockProvider")
        return ExtractionPipeline(MockProvider())
    try:
        provider = DashScopeOCRProvider(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL,
            ocr_model=settings.DASHSCOPE_OCR_MODEL,
            llm_model=settings.DASHSCOPE_LLM_MODEL,
        )
        log.info("DashScopeOCRProvider initialised (model=%s)", provider.model)
        return ExtractionPipeline(provider)
    except ProviderError as e:
        log.warning("DashScopeOCRProvider unavailable (%s); using MockProvider", e)
        return ExtractionPipeline(MockProvider())


async def _periodic_stuck_job_sweep(stop_event: asyncio.Event) -> None:
    """Run recover_stuck_jobs every STUCK_JOB_SWEEP_S until stop_event set.

    Late-binds SessionLocal so tests that monkeypatch the database module
    see the substituted session factory.
    """
    while not stop_event.is_set():
        # Late module attribute lookup so monkeypatched STUCK_JOB_SWEEP_S
        # (used in tests with a tiny interval) takes effect each iteration.
        import apps.api.main as main_mod
        from apps.api.core import database as db_mod

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=main_mod.STUCK_JOB_SWEEP_S)
            return  # event set → exit
        except asyncio.TimeoutError:
            pass
        db = db_mod.SessionLocal()
        try:
            n = DocumentIngestionService.recover_stuck_jobs(
                db,
                max_age_minutes=main_mod.STUCK_JOB_MAX_AGE_MINUTES,
            )
            if n:
                log.warning("Periodic sweep: recovered %d stuck jobs", n)
        except Exception:
            log.exception("Periodic stuck-job sweep failed")
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    pipeline = _build_pipeline()
    app.state.extraction_pipeline = pipeline
    set_runtime_pipeline(pipeline)

    # Recover stuck jobs (startup pass)
    db = SessionLocal()
    try:
        recovered = DocumentIngestionService.recover_stuck_jobs(
            db,
            max_age_minutes=STUCK_JOB_MAX_AGE_MINUTES,
        )
        if recovered:
            log.info("Recovered %d stuck extraction jobs at startup", recovered)
    finally:
        db.close()

    # Start periodic sweep (AUDIT-FIX C3)
    stop_event = asyncio.Event()
    sweep_task = asyncio.create_task(_periodic_stuck_job_sweep(stop_event))

    try:
        yield
    finally:
        # Teardown
        stop_event.set()
        try:
            await asyncio.wait_for(sweep_task, timeout=2.0)
        except asyncio.TimeoutError:
            sweep_task.cancel()
        # Shuts down ThreadPoolExecutor + clears pipeline singleton
        shutdown_runtime()
        app.state.extraction_pipeline = None


app = FastAPI(
    title="MEMPAS API",
    description="机电材料查询比价分析系统",
    version="0.3.0",
    lifespan=lifespan,
)

settings = get_settings()
CORS_ORIGINS = os.getenv("CORS_ORIGINS", settings.CORS_ORIGINS).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in all_routers:
    if router is auth_router:
        app.include_router(router)
    else:
        app.include_router(router, dependencies=[Depends(get_current_user)])


@app.get("/api/health")
def health():
    pipeline = getattr(app.state, "extraction_pipeline", None)
    provider_name = (
        getattr(pipeline.provider, "name", "unknown") if pipeline else "uninitialised"
    )
    return {"status": "ok", "service": "mempas", "llm_provider": provider_name}


@app.get("/api/health/queue")
def health_queue():
    """Extraction thread-pool depth — used to decide when to scale to arq.

    Returns:
      - active_threads: total threads in the pool (busy + idle)
      - queue_depth:    tasks waiting for a free thread (the key signal)
      - max_workers:    configured ceiling

    Operational thresholds (recommend in deployment README):
      - queue_depth > 0 sustained for ~minutes → consider doubling threads
      - queue_depth > max_workers → upgrade to arq + Redis
      - active_threads = max_workers AND queue_depth > 0 sustained → same
    """
    return get_pool_stats()


# Serve Vue SPA static files (production build)
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(STATIC_DIR / "index.html"))
