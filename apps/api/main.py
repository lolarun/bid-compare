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
from apps.api.core.database import SessionLocal, init_db
from apps.api.core.errors import register_exception_handlers
from apps.api.core.runtime import (
    get_pool_stats,
    set_runtime_pipeline,
    shutdown_runtime,
)
from apps.api.core.security import get_current_user
from apps.api.intelligence.base import ProviderError
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.routes import all_routers
from apps.api.routes.auth import router as auth_router
from apps.api.services.ingestion.document_ingestion import DocumentIngestionService

log = logging.getLogger("mempas")

# 2026-08-27：项目159三份文件在"补读缺失金额"卡了7-9分钟、日志上却什么都没有
# ——不是没打日志，是 apps.api.* 的 logger 从没配过 handler/level，INFO 消息
# 走到 root 的 lastResort handler（只认 WARNING+）直接被吞。只给 apps.api 这
# 一支命名空间开 INFO，不动全局 root（不想连 httpx/openai 库自己的调试日志一起
# 打开）。用 `getattr` 判断避免重复 addHandler（uvicorn 无 --reload 时本模块只
# 导入一次，但测试可能多次 import，这里保证幂等）。
_APPS_API_LOG = logging.getLogger("apps.api")
if not getattr(_APPS_API_LOG, "_mempas_configured", False):
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _APPS_API_LOG.addHandler(_handler)
    _APPS_API_LOG.setLevel(logging.INFO)
    # propagate 保持默认 True——不是留漏洞：root 一旦在这条链上遇到过 handler
    # 就不会再补 lastResort，不会重复打印；关掉反而会打断 pytest `caplog`
    # （它挂在 root 上收集记录），两个 test_text_client_switch.py 用例已用
    # 这条实测证实过。
    _APPS_API_LOG._mempas_configured = True

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
        allow_mock = os.environ.get("ALLOW_MOCK_PROVIDER", "true").lower() in ("1", "true", "yes")
        if not allow_mock:
            raise RuntimeError(
                "DASHSCOPE_API_KEY is not set and ALLOW_MOCK_PROVIDER=false — "
                "refusing to start with MockProvider. Set the API key or export ALLOW_MOCK_PROVIDER=true."
            )
        log.warning(
            "DASHSCOPE_API_KEY not set; falling back to MockProvider "
            "(set ALLOW_MOCK_PROVIDER=false to block this in production)"
        )
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
        except TimeoutError:
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

    # Recover stuck jobs (startup pass).
    # 后台识别任务无法跨进程重启续跑：启动时任何 RUNNING/PENDING 的 job 都是上次进程
    # 崩溃/重启遗留的孤儿，必须无视年龄全部回收为 FAILED，否则上传幂等会把这条孤儿
    # 原样返回，用户重传也拿不到新识别（会一直卡在"处理中"）。周期清扫仍用年龄阈值。
    db = SessionLocal()
    try:
        recovered = DocumentIngestionService.recover_stuck_jobs(
            db,
            max_age_minutes=0,
            include_pending=True,
        )
        if recovered:
            log.info("Recovered %d orphaned extraction jobs at startup", recovered)
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
        except TimeoutError:
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
register_exception_handlers(app)  # DomainError → HTTP (评审 E2, core/errors.py)

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
