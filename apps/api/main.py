"""FastAPI application entry point."""

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    pipeline = _build_pipeline()
    app.state.extraction_pipeline = pipeline
    set_runtime_pipeline(pipeline)

    # Recover stuck jobs
    db = SessionLocal()
    try:
        recovered = DocumentIngestionService.recover_stuck_jobs(db)
        if recovered:
            log.info("Recovered %d stuck extraction jobs at startup", recovered)
    finally:
        db.close()

    yield

    # Teardown
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
