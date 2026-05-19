"""FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.core.config import get_settings
from apps.api.core.database import init_db, SessionLocal
from apps.api.core.runtime import set_runtime_pipeline
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.providers.qwen_vl import QwenVLProvider
from apps.api.intelligence.base import ProviderError
from apps.api.routes import all_routers
from apps.api.services.document_ingestion import DocumentIngestionService

log = logging.getLogger("mempas")

# AUDIT-FIX C3: periodic stuck-job recovery beyond the startup pass.
# Background task runs every STUCK_JOB_SWEEP_S and flips any RUNNING job
# whose updated_at is older than the recovery threshold to FAILED.
STUCK_JOB_SWEEP_S = 60

STATIC_DIR = Path(__file__).resolve().parent.parent / "www" / "dist"


def _build_pipeline() -> ExtractionPipeline:
    """Choose provider per LLM_PROVIDER setting; fall back gracefully."""
    settings = get_settings()
    requested = (settings.LLM_PROVIDER or "auto").lower()

    if requested == "mock":
        log.info("LLM_PROVIDER=mock → using MockProvider")
        return ExtractionPipeline(MockProvider())

    if requested in {"auto", "qwen_vl"} and settings.DASHSCOPE_API_KEY:
        try:
            provider = QwenVLProvider(
                api_key=settings.DASHSCOPE_API_KEY,
                base_url=settings.DASHSCOPE_BASE_URL,
                model=settings.LLM_VISION_MODEL,
                models=[settings.LLM_VISION_MODEL_FALLBACK],
            )
            log.info(
                "QwenVLProvider initialised (candidates=%s)", provider.candidates
            )
            return ExtractionPipeline(provider)
        except ProviderError as e:
            log.warning("QwenVLProvider unavailable (%s); falling back to MockProvider", e)
            return ExtractionPipeline(MockProvider())

    log.info("No API key / explicit mock; using MockProvider")
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
            n = DocumentIngestionService.recover_stuck_jobs(db)
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
        recovered = DocumentIngestionService.recover_stuck_jobs(db)
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
        set_runtime_pipeline(None)
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
    app.include_router(router)


@app.get("/api/health")
def health():
    pipeline = getattr(app.state, "extraction_pipeline", None)
    provider_name = (
        getattr(pipeline.provider, "name", "unknown") if pipeline else "uninitialised"
    )
    return {"status": "ok", "service": "mempas", "llm_provider": provider_name}


# Serve Vue SPA static files (production build)
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(STATIC_DIR / "index.html"))
