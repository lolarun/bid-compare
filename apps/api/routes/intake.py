"""Document intake routes — unified upload/status endpoints.

Endpoints:
- POST /api/intake/upload?type=tender|quote  (multipart file + context fields)
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
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.runtime import submit_extraction
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.schemas.intake import JobListResponse, JobResponse
from apps.api.services.document_ingestion import (
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


# Extraction is now dispatched via core/runtime.submit_extraction(), which
# uses a ThreadPoolExecutor sized for IO-bound LLM calls. See runtime.py.
