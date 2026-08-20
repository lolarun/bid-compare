"""Document intake routes — unified upload/status endpoints.

Endpoints:
- POST /api/intake/upload?type=tender|quote  (multipart file + context fields)
- POST /api/intake/enhance                   (AI post-processing: categorize + standardize + align)
- GET  /api/intake/jobs/{job_id}             (poll status)
- GET  /api/intake/jobs                      (list, for admin / debug)
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.runtime import submit_extraction
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.schemas.intake import (
    ClassifyTier0Response,
    JobListResponse, JobResponse,
    EnhanceRequest, EnhanceResponse,
    SummarizeFactsRequest, SummarizeFactsResponse,
)
from apps.api.services.ingestion.document_ingestion import (
    DocumentIngestionService,
    IngestionType,
)

router = APIRouter(prefix="/api/intake", tags=["intake"])


def get_pipeline(request: Request) -> ExtractionPipeline:
    """Resolve the global ExtractionPipeline from app.state.

    Raises 503 if not initialised (e.g. provider failed to construct).
    """
    pipeline = getattr(request.app.state, "extraction_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Extraction pipeline not initialised")
    return pipeline


@router.post("/upload", response_model=JobResponse)
async def upload_document(
    file: UploadFile = File(...),
    type: str = Form(...),
    project_id: Optional[int] = Form(None),
    supplier_id: Optional[int] = Form(None),
    category: Optional[str] = Form(None),
    context_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    pipeline: ExtractionPipeline = Depends(get_pipeline),
) -> JobResponse:
    """Create an extraction job and queue it on the thread pool.

    HTTP returns immediately (<100 ms typical). Caller polls
    GET /api/intake/jobs/{id} for status (pending → running → done/failed).

    AUDIT-FIX L4: previously used FastAPI BackgroundTasks which runs after
    response on the same event loop, blocking the worker. Now uses a
    dedicated ThreadPoolExecutor — see core/runtime.py.
    """
    if type not in {t.value for t in IngestionType}:
        raise HTTPException(status_code=400, detail=f"Invalid type: {type}")

    # Build context dict from explicit form fields, allowing context_json override
    context: dict = {}
    if project_id is not None:
        context["project_id"] = project_id
    if supplier_id is not None:
        context["supplier_id"] = supplier_id
    if category:
        context["category"] = category
    if context_json:
        try:
            extra = json.loads(context_json)
            if isinstance(extra, dict):
                context.update(extra)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="context_json is not valid JSON")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file upload")

    service = DocumentIngestionService(db, pipeline)
    try:
        job = service.create_job(content, file.filename or "upload", type, context)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Only queue if the job is brand new — idempotent hits returning a
    # DONE/RUNNING job should NOT re-run.
    if job.status == "pending":
        submit_extraction(job.id)

    return JobResponse.model_validate(job)


@router.post("/classify-tier0", response_model=ClassifyTier0Response)
async def classify_tier0_upload(file: UploadFile = File(...)) -> ClassifyTier0Response:
    """design/28 §3 Tier 0 + design/29 §3 Tier 1.5——拖进来的文件是招标/
    投标/清单哪一种，瞬时判定，不建 ExtractionJob、不进识别队列。cut 5
    拖拽确认屏的第一级判据来源；xlsx/pdf 判不出来时都是合法答案，不是
    接口异常——pdf 扫描件恒为 uncertain（design/29 §3.1：视觉判定实测
    0/7，不调用，直接留给前端弹窗二选一），不是"这个接口还没做完"。
    """
    import tempfile
    from pathlib import Path as _Path

    from apps.api.intelligence.document_classify import (
        ExcelClassification, PdfClassification, classify_tier0,
    )
    from apps.api.intelligence.scanned_pdf_classify import classify_pdf_for_dispatch

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file upload")

    suffix = _Path(file.filename or "").suffix.lower()
    # classify_tier0 按扩展名读文件，需要真实落盘路径（openpyxl/pypdfium2
    # 都不接受内存 buffer 直接当路径用）；用临时文件，判完即删，不留痕。
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = classify_tier0(tmp_path)
        pdf_kind = (classify_pdf_for_dispatch(tmp_path, call=None)
                    if isinstance(result, PdfClassification) else None)
    finally:
        _Path(tmp_path).unlink(missing_ok=True)

    filename = file.filename or "upload"
    if isinstance(result, ExcelClassification):
        return ClassifyTier0Response(
            filename=filename, kind="excel", verdict=result.verdict,
            confidence=result.confidence, price_columns=result.price_columns,
            fill_rate=result.fill_rate, row_count=result.row_count, reason=result.reason,
        )
    if isinstance(result, PdfClassification):
        return ClassifyTier0Response(
            filename=filename, kind="pdf", verdict=pdf_kind.verdict,
            text_layer=result.text_layer, reason=pdf_kind.reason or result.reason,
        )
    return ClassifyTier0Response(
        filename=filename, kind="unsupported", verdict="unsupported",
        reason=f"不支持的文件类型：{suffix or '(无扩展名)'}",
    )


@router.post("/summarize-facts", response_model=SummarizeFactsResponse)
async def summarize_facts(body: SummarizeFactsRequest) -> SummarizeFactsResponse:
    """design/29 §4——工作台卡片概述。只读已确认事实，不碰识别、不新增
    模型调用成本量级（沿用 paddle_doc_meta 已在用的纯文本客户端）。"""
    from apps.api.intelligence.document_summary import compose_summary
    from apps.api.intelligence.paddle_doc_meta import get_text_client_call

    if body.kind not in ("tender", "bid"):
        raise HTTPException(status_code=400, detail=f"kind 必须是 tender 或 bid，收到：{body.kind}")

    summary = compose_summary(body.kind, body.facts, get_text_client_call())
    return SummarizeFactsResponse(summary=summary)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    pipeline: ExtractionPipeline = Depends(get_pipeline),
) -> JobResponse:
    service = DocumentIngestionService(db, pipeline)
    job = service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobResponse.model_validate(job)


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    pipeline: ExtractionPipeline = Depends(get_pipeline),
) -> JobListResponse:
    service = DocumentIngestionService(db, pipeline)
    jobs = service.list_jobs(type=type, status=status, limit=limit)
    return JobListResponse(
        items=[JobResponse.model_validate(j) for j in jobs],
        total=len(jobs),
    )


@router.post("/enhance", response_model=EnhanceResponse)
def enhance_extraction(
    body: EnhanceRequest,
    db: Session = Depends(get_db),
) -> EnhanceResponse:
    """AI-enhanced post-processing of OCR results.

    Call after OCR extraction is done (job status=done) and before batch-confirm.
    Adds per-item category, standardized names, and pre-alignment flags.

    Two input modes:
      1. Pass ``job_id`` — loads items from the completed job's result.
      2. Pass ``items`` directly — skips job lookup (useful for testing).

    Optionally pass ``project_id`` to enable pre-alignment against existing
    project quotes from other suppliers.
    """
    from apps.api.services.ingestion.enhance import enhance_ocr_items

    items: list[dict] = []

    if body.items is not None:
        items = body.items
    elif body.job_id:
        # Load from job
        from apps.api.models.extraction_job import ExtractionJob
        job = db.scalar(select(ExtractionJob).where(ExtractionJob.id == body.job_id))
        if not job:
            raise HTTPException(404, f"Job {body.job_id} not found")
        if job.status != "done":
            raise HTTPException(400, f"Job not done yet (status={job.status})")
        result = job.result or {}
        items = result.get("items", [])
        if not items:
            raise HTTPException(400, "Job has no items to enhance")
    else:
        raise HTTPException(400, "Provide job_id or items")

    result = enhance_ocr_items(items, body.project_id, db)
    return EnhanceResponse(**result)


# Extraction is now dispatched via core/runtime.submit_extraction(), which
# uses a ThreadPoolExecutor sized for IO-bound LLM calls. See runtime.py.
