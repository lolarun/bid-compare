"""Quote CRUD API endpoints."""

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

log = logging.getLogger(__name__)

from apps.api.core.database import get_db
from apps.api.models import (
    ExtractionJob,
    Material,
    Project,
    Quote,
    Supplier,
    BidSubmission,
    BidQuoteLine,
)
from apps.api.schemas import (
    QuoteCreate, QuoteUpdate, QuoteOut, ImportResult, BatchConfirmResult,
    QuoteListResult, QuoteBatchListResult, QuoteStatsResult, ArchivePricesResult,
)
from apps.api.services.ingestion.import_service import import_csv_data, _gen_code

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


class BatchConfirmRequest(BaseModel):
    """暂存一次 OCR 提取的报价 → BidSubmission + BidQuoteLine（弱关联版）。

    弱关联规则：
    - supplier_id 可选；有则作为软引用（须为 active Supplier）；无则陌生供应商直接暂存。
    - supplier_name 必填：作为比价时的显示名（写入 supplier_raw_name）。
    - batch_id = BID-{job.id}：一个 job 最多产生一条 BidSubmission（幂等）。
    - Material 未找到时 material_id=NULL，仍写入 BidQuoteLine（禁止自动创建 Material）。
    - 归档到 Quote 须调用 archive-prices，且 supplier_id 必须非空。
    """

    job_id: str
    # 声明总价闭环门的显式放行：声明总价含清单外项目（税费/优惠）时才该用。
    # 与 total_is_manual / integrity_ack 一样，系统绝不替用户做这个判断。
    checksum_ack: bool = False
    supplier_id: int | None = None            # 可选：软引用已知供应商
    supplier_name: str = ""                   # 必填：比价显示名（unknown supplier 时为 OCR 原始名）
    project_id: int | None = None
    project_name: str = ""                    # 查找现有 project（不自动创建）
    category: str = ""
    overrides: list[dict[str, Any]] | None = None
    bid_status: str = ""
    # design/24 B3：预演——跑一遍完全相同的判据，从不写库，把这份文档所有的
    # 结构性疑点一次性收集返回，而不是等用户真点「校对入库」才逐个撞见。
    dry_run: bool = False


@router.get("", response_model=QuoteListResult)
def list_quotes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    material_id: int | None = None,
    supplier_id: int | None = None,
    project_id: int | None = None,
    category: str | None = None,
    profession: str | None = None,
    keyword: str | None = None,
    alert_level: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Quote).options(
        selectinload(Quote.material),
        selectinload(Quote.supplier),
        selectinload(Quote.project),
    )
    if material_id:
        stmt = stmt.where(Quote.material_id == material_id)
    if supplier_id:
        stmt = stmt.where(Quote.supplier_id == supplier_id)
    if project_id:
        stmt = stmt.where(Quote.project_id == project_id)
    if category:
        stmt = stmt.join(Material, isouter=True).where(Material.category == category)
    if profession:
        if not category:
            stmt = stmt.join(Material, isouter=True)
        stmt = stmt.where(Material.profession == profession)
    if keyword:
        if not category and not profession:
            stmt = stmt.join(Material, isouter=True)
        stmt = stmt.where(
            Material.standard_name.contains(keyword)
            | Material.spec.contains(keyword)
        )
    if alert_level:
        stmt = stmt.where(Quote.alert_level == alert_level)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Quote.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()

    result_items = []
    for i in items:
        d = QuoteOut.model_validate(i).model_dump()
        # Eagerly loaded relations → flatten for frontend
        if i.material:
            d["material_name"] = i.material.standard_name
            d["spec"] = i.material.spec or ""
            d["unit"] = i.material.unit or ""
            d["category"] = i.material.category or ""
            d["profession"] = i.material.profession or ""
        if i.supplier:
            d["supplier_name"] = i.supplier.name
        if i.project:
            d["project_name"] = i.project.name
        result_items.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": result_items,
    }


# ─── Batches ──────────────────────────────────────────────────────────────────

@router.get("/batches", response_model=QuoteBatchListResult)
def list_batches(
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(
            Quote.batch_id,
            func.count(Quote.id).label("count"),
            func.min(Quote.created_at).label("created_at"),
            func.max(Quote.supplier_id).label("supplier_id"),
            func.max(Quote.project_id).label("project_id"),
        )
        .where(Quote.batch_id.isnot(None), Quote.batch_id != "")
        .group_by(Quote.batch_id)
        .order_by(func.min(Quote.created_at).desc())
    ).all()
    items = []
    for r in rows:
        supplier = db.get(Supplier, r.supplier_id) if r.supplier_id else None
        project = db.get(Project, r.project_id) if r.project_id else None
        items.append({
            "batch_id": r.batch_id,
            "count": r.count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "supplier_id": r.supplier_id,
            "supplier_name": supplier.name if supplier else "",
            "project_id": r.project_id,
            "project_name": project.name if project else "",
        })
    return {"items": items, "total": len(items)}


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: str, db: Session = Depends(get_db)):
    count = db.execute(delete(Quote).where(Quote.batch_id == batch_id)).rowcount
    db.commit()
    if count == 0:
        raise HTTPException(404, f"Batch {batch_id} not found")
    return {"deleted": count}


# ─── BidSubmission 软删除（比价暂存层移除，须在 /{quote_id} 之前注册）──────────
# 标记 status=superseded 而非物理删除（CLAUDE.md §12：优先标记不物删）。
# compare-state 过滤 superseded → 前端刷新后不再出现；重新上传同一文件会复活
# （见 batch-confirm 复活分支）；BidQuoteLine 保留，复活时清理重建。
_ACTIVE_SUBMISSION_STATUSES = ["superseded", "rejected"]


@router.delete("/submissions/{submission_id}")
def supersede_submission(submission_id: int, db: Session = Depends(get_db)):
    """逐个移除：把单条比价暂存 submission 标记 superseded（软删除，可复活）。"""
    sub = db.get(BidSubmission, submission_id)
    if sub is None:
        raise HTTPException(404, f"Submission {submission_id} not found")
    if sub.status == "superseded":
        return {"submission_id": submission_id, "status": "superseded", "already": True}
    sub.status = "superseded"
    # 同步 job 生命周期 → removed，避免其作为在途任务重新出现在 compare-state
    if sub.job_id:
        job = db.get(ExtractionJob, sub.job_id)
        if job:
            job.lifecycle = "removed"
    db.commit()
    log.info("supersede_submission: submission_id=%d → superseded", submission_id)
    return {"submission_id": submission_id, "status": "superseded", "already": False}


@router.delete("/submissions")
def supersede_project_submissions(
    project_id: int = Query(...), db: Session = Depends(get_db)
):
    """一键移除：把某项目下全部 active submission 标记 superseded（软删除，可复活）。"""
    subs = db.scalars(
        select(BidSubmission).where(
            BidSubmission.project_id == project_id,
            BidSubmission.status.notin_(_ACTIVE_SUBMISSION_STATUSES),
        )
    ).all()
    ids = [s.id for s in subs]
    job_ids = [s.job_id for s in subs if s.job_id]
    for s in subs:
        s.status = "superseded"
    # 同步对应 job 生命周期 → removed（在途 job 无 submission，不受影响）
    if job_ids:
        db.execute(update(ExtractionJob).where(ExtractionJob.id.in_(job_ids)).values(lifecycle="removed"))
    db.commit()
    log.info(
        "supersede_project_submissions: project_id=%d superseded %d submissions %s",
        project_id, len(ids), ids,
    )
    return {"superseded_ids": ids, "count": len(ids)}


@router.delete("/jobs/{job_id}")
def remove_job(job_id: str, db: Session = Depends(get_db)):
    """移除在途/失败的识别任务：标记 ExtractionJob.lifecycle=removed（软删，不物删）。

    用于失败或"已识别待确认"的报价文件卡片移除（这类 job 无 BidSubmission，
    不走 supersede 路径）。compare-state 据此不再作为在途返回。
    注意：若任务仍 running，仅隐藏，不强制中断后台线程（避免半完成状态）。
    """
    job = db.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} not found")
    if job.lifecycle == "removed":
        return {"job_id": job_id, "lifecycle": "removed", "already": True}
    job.lifecycle = "removed"
    db.commit()
    log.info("remove_job: job_id=%s → removed (ocr_status=%s)", job_id, job.status)
    return {"job_id": job_id, "lifecycle": "removed", "already": False}


# ─── Stats (must be before /{quote_id} to avoid route conflict) ────────────

@router.get("/stats", response_model=QuoteStatsResult)
def quote_stats(
    category: str | None = None,
    supplier_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Get aggregate quote statistics."""
    stmt = select(Quote).where(Quote.unit_price > 0)
    if category:
        stmt = stmt.join(Material).where(Material.category == category)
    if supplier_id:
        stmt = stmt.where(Quote.supplier_id == supplier_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if total == 0:
        return {"total": 0, "avg_price": None, "min_price": None, "max_price": None,
                "alert_counts": {"normal": 0, "yellow": 0, "red": 0}}

    base_stmt = select(
        func.avg(Quote.unit_price),
        func.min(Quote.unit_price),
        func.max(Quote.unit_price),
    ).where(Quote.unit_price > 0)
    if category:
        base_stmt = base_stmt.join(Material).where(Material.category == category)
    if supplier_id:
        base_stmt = base_stmt.where(Quote.supplier_id == supplier_id)
    avg_p, min_p, max_p = db.execute(base_stmt).one()

    alert_stmt = select(Quote.alert_level, func.count(Quote.id)).where(Quote.unit_price > 0)
    if category:
        alert_stmt = alert_stmt.join(Material).where(Material.category == category)
    if supplier_id:
        alert_stmt = alert_stmt.where(Quote.supplier_id == supplier_id)
    alert_rows = db.execute(alert_stmt.group_by(Quote.alert_level)).all()
    alerts = {"normal": 0, "yellow": 0, "red": 0}
    for level, cnt in alert_rows:
        if level in alerts:
            alerts[level] = cnt

    return {
        "total": total,
        "avg_price": round(float(avg_p), 2) if avg_p else None,
        "min_price": round(float(min_p), 2) if min_p else None,
        "max_price": round(float(max_p), 2) if max_p else None,
        "alert_counts": alerts,
    }


@router.get("/{quote_id}", response_model=QuoteOut)
def get_quote(quote_id: int, db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    return quote


@router.post("", response_model=QuoteOut, status_code=201)
def create_quote(body: QuoteCreate, db: Session = Depends(get_db)):
    mat = db.get(Material, body.material_id)
    if not mat:
        raise HTTPException(400, f"Material {body.material_id} not found")

    quote = Quote(**body.model_dump())

    # 自动计算 total_price
    if quote.unit_price and quote.quantity:
        quote.total_price = round(quote.unit_price * quote.quantity, 4)

    # 偏差率 & 色标（使用合理史低）
    ref = mat.ref_price_reasonable_low or mat.ref_price_median
    if quote.unit_price and ref and ref > 0:
        from apps.api.services.history.comparison import get_category_thresholds, determine_alert
        quote.deviation_pct = round((quote.unit_price - ref) / ref, 4)
        thresholds = get_category_thresholds(db, mat.category)
        quote.alert_level = determine_alert(quote.deviation_pct, thresholds)

    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


@router.put("/{quote_id}", response_model=QuoteOut)
def update_quote(quote_id: int, body: QuoteUpdate, db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(quote, field, value)

    db.commit()
    db.refresh(quote)
    return quote


@router.delete("/{quote_id}", status_code=204)
def delete_quote(quote_id: int, db: Session = Depends(get_db)):
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    db.delete(quote)
    db.commit()


# ─── Import ─────────────────────────────────────────────────────────────────

@router.post("/import", response_model=ImportResult)
async def import_file(
    file: UploadFile = File(...),
    category: str = Form(...),
    project_name: str = Form(""),
    project_id: int | None = Form(None),
    supplier_id: int | None = Form(None),
    bid_status: str = Form(""),
    db: Session = Depends(get_db),
):
    """Import a CSV or Excel file, creating Material + Quote records."""
    if not file.filename:
        raise HTTPException(400, "No file provided")
    if not file.filename.endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(400, "Only .csv, .xlsx, .xls files are supported")

    if project_id and not project_name:
        proj = db.get(Project, project_id)
        if proj:
            project_name = proj.name

    content = await file.read()
    result = import_csv_data(
        db, content, file.filename, category, project_name,
        default_supplier_id=supplier_id,
        bid_status=bid_status,
    )
    if result["status"] == "error" and result["imported"] == 0:
        raise HTTPException(422, detail=result)
    return result


# ─── Batch confirm (P0 新版): ExtractionJob.result → BidSubmission + BidQuoteLine ──
@router.post("/batch-confirm", response_model=BatchConfirmResult)
def batch_confirm(body: BatchConfirmRequest = Body(...), db: Session = Depends(get_db)):
    """将 OCR 提取结果暂存为 BidSubmission + BidQuoteLine（P0 新版）。

    关键约束（P0）：
    - supplier_id 必须由前端明确传入，禁止自动创建 Supplier。
    - Material 未找到时 material_id=NULL，仍写入 BidQuoteLine（禁止创建 Material）。
    - 本函数不再写入 Quote / Material / Supplier 历史表。
    - 归档到 Quote 须显式调用 POST /api/quotes/archive-prices。
    """
    from apps.api.services.submission.quote_confirmation_service import confirm_batch
    return confirm_batch(db, body, dry_run=body.dry_run)


# ─── Archive prices: BidSubmission → Quote（显式归档）────────────────────────
class ArchivePricesRequest(BaseModel):
    """将 BidSubmission 中 material_id 非空的行归档为 Quote 历史价格记录。"""

    submission_id: int
    project_id: int | None = None  # 覆盖 BidSubmission.project_id（可选）


@router.post("/archive-prices", response_model=ArchivePricesResult)
def archive_prices(body: ArchivePricesRequest, db: Session = Depends(get_db)):
    """将 BidSubmission 中 material_id 非空的 BidQuoteLine 归档为 Quote。

    归档规则（P0）：
    - material_id IS NULL 的行静默跳过（不报错，不创建 Material）。
    - archived_quote_id 非空的行（已归档）跳过。
    - 只创建 Quote，不创建 Material / Supplier。
    - 成功归档后回填 BidQuoteLine.archived_quote_id。
    - 返回三态 status: archived / partially_archived / no_eligible。
    """
    submission = db.get(BidSubmission, body.submission_id)
    if not submission:
        raise HTTPException(404, f"BidSubmission {body.submission_id} not found")
    if submission.status == "archived":
        raise HTTPException(409, f"Submission {body.submission_id} is already fully archived")
    if submission.supplier_id is None:
        raise HTTPException(
            422,
            "归档需要绑定正式供应商 (supplier_id)。"
            "请先在供应商管理中创建该供应商并重新入库。",
        )

    lines = db.scalars(
        select(BidQuoteLine).where(BidQuoteLine.submission_id == submission.id)
    ).all()

    # Null-material lines can never be archived — include in skipped_lines with reason
    null_material_lines = [ln for ln in lines if ln.material_id is None]
    null_skipped: list[dict] = [
        {"line_id": ln.id, "reason": "material_id is NULL — cannot archive without material link"}
        for ln in null_material_lines
    ]

    eligible = [
        ln for ln in lines
        if ln.material_id is not None and ln.archived_quote_id is None
    ]
    already_archived_count = sum(
        1 for ln in lines
        if ln.material_id is not None and ln.archived_quote_id is not None
    )
    eligible_count = len(eligible)

    project_id = body.project_id or submission.project_id
    archived_count = 0
    error_skipped: list[dict] = []

    for line in eligible:
        try:
            q = Quote(
                material_id=line.material_id,
                supplier_id=submission.supplier_id,
                project_id=project_id,
                unit_price=line.unit_price,
                unit_price_excl_tax=line.unit_price_excl_tax,
                quantity=line.qty,
                total_price=line.total_price,
                tax_rate=line.tax_rate,
                brand=line.brand,
                brand_tier=line.brand_tier,
                remark=line.remark,
                quote_date=line.quote_date,
                batch_id=f"ARCH-{submission.id}-{line.id}",
                bid_status=submission.bid_status,
                extraction_meta_json=line.extraction_meta,
                deviation_pct=line.deviation_pct,
                alert_level=line.alert_level,
            )
            db.add(q)
            db.flush()
            line.archived_quote_id = q.id
            archived_count += 1
        except Exception as e:
            error_skipped.append({"line_id": line.id, "reason": f"{type(e).__name__}: {e}"})

    skipped_lines = null_skipped + error_skipped

    # Status is based on ALL submission lines:
    # "no_eligible"        — eligible_count=0 AND no previously archived lines
    # "archived"           — zero null-material lines AND all eligible archived without error
    # "partially_archived" — any other case (null lines, errors, or partial archival)
    if eligible_count == 0 and already_archived_count == 0:
        status = "no_eligible"
    elif not null_skipped and not error_skipped and archived_count == eligible_count:
        status = "archived"
    else:
        status = "partially_archived"

    submission.status = status
    db.commit()

    return {
        "status": status,
        "submission_id": submission.id,
        "eligible_count": eligible_count,
        "archived_count": archived_count,
        "skipped_count": len(skipped_lines),
        "already_archived_count": already_archived_count,
        "skipped_lines": skipped_lines,
    }
