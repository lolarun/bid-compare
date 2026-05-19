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
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
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
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    type: str = Form(...),
    project_id: Optional[int] = Form(None),
    supplier_id: Optional[int] = Form(None),
    category: Optional[str] = Form(None),
    context_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    pipeline: ExtractionPipeline = Depends(get_pipeline),
) -> JobResponse:
    """Create an extraction job and schedule background execution.

    Returns immediately; poll GET /jobs/{id} for status.
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

    # Only schedule background work if the job is brand new (pending).
    # Idempotent hits returning a DONE/RUNNING job should NOT re-run.
    if job.status == "pending":
        background_tasks.add_task(_run_job_in_background, job.id)

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


# ─── background task wrapper ──────────────────────────────────────────────
def _run_job_in_background(job_id: str) -> None:
    """Free-function so BackgroundTasks pickles it cleanly.

    Creates its own DB session and pipeline reference inside the task —
    do NOT capture the request's `db` or `pipeline` (they go out of scope).
    """
    from apps.api.core.database import SessionLocal
    from apps.api.core.runtime import get_runtime_pipeline

    pipeline = get_runtime_pipeline()
    if pipeline is None:
        # No pipeline available — write failure to job
        db = SessionLocal()
        try:
            from apps.api.models import ExtractionJob
            from apps.api.services.document_ingestion import JobStatus

            job = db.get(ExtractionJob, job_id)
            if job:
                job.status = JobStatus.FAILED.value
                job.error = "Extraction pipeline not available at task time"
                db.commit()
        finally:
            db.close()
        return

    db = SessionLocal()
    try:
        service = DocumentIngestionService(db, pipeline)
        service.run_job(job_id)
    finally:
        db.close()
