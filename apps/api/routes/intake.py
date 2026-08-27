"""Document intake routes — unified upload/status endpoints.

Endpoints:
- POST /api/intake/upload?type=tender|quote  (multipart file + context fields)
- POST /api/intake/enhance                   (AI post-processing: categorize + standardize + align)
- GET  /api/intake/jobs/{job_id}             (poll status)
- GET  /api/intake/jobs                      (list, for admin / debug)
"""

from __future__ import annotations

import json
from pathlib import Path
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
def upload_document(
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

    **本函数必须是 `def` 而不是 `async def`**（2026-08-22）：`create_job` 是
    同步阻塞的——SHA256 整个文件、写盘、再写一条 SQLite（要抢写锁，识别任务
    正在不停提交进度）。写在 `async def` 里，这段阻塞卡的是**整个事件循环**，
    期间服务器不处理任何请求：实测表现为上传 60s 超时、同一时刻的
    `PUT /api/projects` 也一起超时。改成 `def` 之后 FastAPI 会把它放进线程池，
    只占一个线程，别的请求照常服务。见 `test_intake_routes_not_async.py`。
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

    content = file.file.read()
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
def classify_tier0_upload(file: UploadFile = File(...)) -> ClassifyTier0Response:
    """design/28 §3 Tier 0 + design/29 §3 Tier 1.5——拖进来的文件是招标/
    投标/清单哪一种，瞬时判定，不建 ExtractionJob、不进识别队列。cut 5
    拖拽确认屏的第一级判据来源；xlsx/pdf 判不出来时都是合法答案，不是
    接口异常。

    2026-08-21 修正：pdf 扫描件此前恒为 uncertain（design/29 §3.1 最初
    实测视觉判定 0/7，接口层直接不调用）——那版判据只送第一页缩略图，
    改成送前几页原生分辨率图 + 修正提示词后同批语料复测 8/8，接口这里
    也改成真的调用（`get_scanned_classify_call()` 未配置 API key 时仍然
    优雅退化成 uncertain，不是新增了一个失败点）。
    """
    import tempfile
    from pathlib import Path as _Path

    from apps.api.intelligence.document_classify import (
        ExcelClassification, PdfClassification, classify_tier0,
    )
    from apps.api.intelligence.scanned_pdf_classify import (
        classify_pdf_for_dispatch, get_scanned_classify_call,
    )

    # `def` 而不是 `async def`：下面 classify_pdf_for_dispatch 会发一次**真实
    # 视觉调用**（实测 6.5-9 秒），classify_tier0 还要 pdfium 渲染。放在事件
    # 循环里等于每分类一份文件就把整个服务器冻住那么久。
    content = file.file.read()
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
        pdf_kind = (classify_pdf_for_dispatch(tmp_path, call=get_scanned_classify_call())
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
def summarize_facts(body: SummarizeFactsRequest) -> SummarizeFactsResponse:
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


@router.post("/jobs/{job_id}/re-recognize", response_model=JobResponse)
def re_recognize_job(
    job_id: str,
    db: Session = Depends(get_db),
    pipeline: ExtractionPipeline = Depends(get_pipeline),
) -> JobResponse:
    """拿这个 job 已经存下来的原文件**重新识别一遍**，返回新 job。

    为什么要有这个入口（2026-08-25）：`create_job` 的幂等键是
    `(file_hash, type, context_hash)`，**里面没有代码版本**。识别逻辑改好之后
    重传同一份文件会命中旧 job、拿回改动前的旧结果——本轮已经咬过两次（项目
    106 的品类，以及三份报价单的"明细合计 ¥0"）。在此之前用户唯一的出路是
    **新建项目**换掉 context_hash，既不直观，也留下一堆废项目。

    走的是**已存盘的原文件**（`job.file_path`），不需要用户重新上传——重传
    反而会再次撞上同一个幂等键。新 job 沿用旧 job 的 `type` 和 `context`，
    所以下游（供应商归属、项目、品类）完全不变，只是识别结果是新的。

    **旧 job 不动**：不删不改，留作对照和审计。识别是要花钱的，重跑必须是
    用户显式点出来的动作，不能由系统自作主张。
    """
    service = DocumentIngestionService(db, pipeline)
    old = service.get_job(job_id)
    if not old:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not old.file_path or not Path(old.file_path).exists():
        raise HTTPException(
            status_code=410,
            detail={"error": "source_file_gone",
                    "message": "原文件已不在服务器上，无法重新识别，请重新上传该文件。"},
        )

    content = Path(old.file_path).read_bytes()
    job = service.create_job(content, old.filename or "upload", old.type,
                             old.context or {}, force=True)
    if job.status == "pending":
        submit_extraction(job.id)
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
