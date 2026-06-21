"""Quote CRUD API endpoints."""

import logging
import re
from typing import Any

# Grand-total/subtotal name patterns — keep in sync with table_parser._GRAND_TOTAL_KEYWORDS.
# Used as a last-resort DB guard to block aggregate rows that slipped through OCR extraction.
_GRAND_TOTAL_NAME_RE = re.compile(
    r"价税合计|总计|合计金额|投标总价|^合计$|含税总计|含税合计|详见投标清单"
)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

log = logging.getLogger(__name__)

from apps.api.core.config import PROFESSION_MAP
from apps.api.core.database import get_db
from apps.api.models import (
    BrandTier,
    ExtractionJob,
    Material,
    Project,
    Quote,
    Supplier,
    BidSubmission,
    BidQuoteLine,
)
from apps.api.schemas import QuoteCreate, QuoteUpdate, QuoteOut, ImportResult
from apps.api.services.import_service import import_csv_data, _gen_code
from apps.api.services.standardize import standardize_name
from apps.api.intelligence.price_basis import derive_price_basis

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


def _num_or_none(v: Any) -> float | None:
    """Coerce to float, preserving None (used for extraction_meta raw价字段)。"""
    return float(v) if v is not None else None


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
    supplier_id: int | None = None            # 可选：软引用已知供应商
    supplier_name: str = ""                   # 必填：比价显示名（unknown supplier 时为 OCR 原始名）
    project_id: int | None = None
    project_name: str = ""                    # 查找现有 project（不自动创建）
    category: str = ""
    overrides: list[dict[str, Any]] | None = None
    bid_status: str = ""


@router.get("", response_model=dict)
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
    q = db.query(Quote).options(
        selectinload(Quote.material),
        selectinload(Quote.supplier),
        selectinload(Quote.project),
    )
    if material_id:
        q = q.filter(Quote.material_id == material_id)
    if supplier_id:
        q = q.filter(Quote.supplier_id == supplier_id)
    if project_id:
        q = q.filter(Quote.project_id == project_id)
    if category:
        q = q.join(Material, isouter=True).filter(Material.category == category)
    if profession:
        if not category:
            q = q.join(Material, isouter=True)
        q = q.filter(Material.profession == profession)
    if keyword:
        if not category and not profession:
            q = q.join(Material, isouter=True)
        q = q.filter(
            Material.standard_name.contains(keyword)
            | Material.spec.contains(keyword)
        )
    if alert_level:
        q = q.filter(Quote.alert_level == alert_level)

    total = q.count()
    items = q.order_by(Quote.id.desc()).offset((page - 1) * page_size).limit(page_size).all()

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

@router.get("/batches", response_model=dict)
def list_batches(
    db: Session = Depends(get_db),
):
    rows = (
        db.query(
            Quote.batch_id,
            func.count(Quote.id).label("count"),
            func.min(Quote.created_at).label("created_at"),
            func.max(Quote.supplier_id).label("supplier_id"),
            func.max(Quote.project_id).label("project_id"),
        )
        .filter(Quote.batch_id.isnot(None), Quote.batch_id != "")
        .group_by(Quote.batch_id)
        .order_by(func.min(Quote.created_at).desc())
        .all()
    )
    items = []
    for r in rows:
        supplier = db.query(Supplier).get(r.supplier_id) if r.supplier_id else None
        project = db.query(Project).get(r.project_id) if r.project_id else None
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
    count = db.query(Quote).filter(Quote.batch_id == batch_id).delete()
    db.commit()
    if count == 0:
        raise HTTPException(404, f"Batch {batch_id} not found")
    return {"deleted": count}


# ─── Stats (must be before /{quote_id} to avoid route conflict) ────────────

@router.get("/stats", response_model=dict)
def quote_stats(
    category: str | None = None,
    supplier_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Get aggregate quote statistics."""
    q = db.query(Quote).filter(Quote.unit_price > 0)
    if category:
        q = q.join(Material).filter(Material.category == category)
    if supplier_id:
        q = q.filter(Quote.supplier_id == supplier_id)

    total = q.count()
    if total == 0:
        return {"total": 0, "avg_price": None, "min_price": None, "max_price": None,
                "alert_counts": {"normal": 0, "yellow": 0, "red": 0}}

    base_q = db.query(
        func.avg(Quote.unit_price),
        func.min(Quote.unit_price),
        func.max(Quote.unit_price),
    ).filter(Quote.unit_price > 0)
    if category:
        base_q = base_q.join(Material).filter(Material.category == category)
    if supplier_id:
        base_q = base_q.filter(Quote.supplier_id == supplier_id)
    avg_p, min_p, max_p = base_q.one()

    alert_q = db.query(Quote.alert_level, func.count(Quote.id)).filter(
        Quote.unit_price > 0
    )
    if category:
        alert_q = alert_q.join(Material).filter(Material.category == category)
    if supplier_id:
        alert_q = alert_q.filter(Quote.supplier_id == supplier_id)
    alert_rows = alert_q.group_by(Quote.alert_level).all()
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
        from apps.api.services.comparison import get_category_thresholds, determine_alert
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
@router.post("/batch-confirm", response_model=dict)
def batch_confirm(body: BatchConfirmRequest = Body(...), db: Session = Depends(get_db)):
    """将 OCR 提取结果暂存为 BidSubmission + BidQuoteLine（P0 新版）。

    关键约束（P0）：
    - supplier_id 必须由前端明确传入，禁止自动创建 Supplier。
    - Material 未找到时 material_id=NULL，仍写入 BidQuoteLine（禁止创建 Material）。
    - 本函数不再写入 Quote / Material / Supplier 历史表。
    - 归档到 Quote 须显式调用 POST /api/quotes/archive-prices。
    """
    job = db.get(ExtractionJob, body.job_id)
    if not job:
        raise HTTPException(404, f"Job {body.job_id} not found")
    if job.type != "quote":
        raise HTTPException(400, f"Job type is {job.type}; must be 'quote'")
    if job.status != "done":
        raise HTTPException(400, f"Job status is {job.status}; must be 'done'")

    # ── Supplier 验证（弱关联：supplier_id 可选，有则校验状态）──────────────────
    supplier: Supplier | None = None
    if body.supplier_id is not None:
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier:
            raise HTTPException(404, f"Supplier {body.supplier_id} not found")
        if supplier.merge_status != "active":
            raise HTTPException(
                400,
                f"Supplier {supplier.name!r} merge_status={supplier.merge_status}，"
                "只允许选择 active 供应商",
            )
    else:
        # 陌生供应商：supplier_name 必填
        if not body.supplier_name.strip():
            raise HTTPException(422, "陌生供应商必须提供 supplier_name")

    # ── Project（仍允许按名查找或创建，project 不是污染来源）────────────────────
    project: Project | None = None
    if body.project_id:
        project = db.get(Project, body.project_id)
        if not project:
            raise HTTPException(404, f"Project {body.project_id} not found")
    elif body.project_name.strip():
        pname = body.project_name.strip()
        project = db.query(Project).filter_by(name=pname).first()
        if not project:
            project = Project(name=pname)
            db.add(project)
            db.flush()
    elif (job.context or {}).get("project_id"):
        ctx_pid = job.context["project_id"]
        project = db.get(Project, ctx_pid)
        if not project:
            raise HTTPException(
                400,
                f"Project {ctx_pid} from job context no longer exists; "
                "specify project_name or project_id to proceed.",
            )

    # ── 默认 category ──────────────────────────────────────────────────────────
    default_category = (
        body.category.strip()
        or (job.context or {}).get("category", "")
        or ""
    )
    if default_category and default_category not in PROFESSION_MAP:
        raise HTTPException(400, f"Unknown category: {default_category}")

    # ── Item list ──────────────────────────────────────────────────────────────
    raw_items: Any = (
        body.overrides
        if body.overrides is not None
        else (job.result or {}).get("items")
    )
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise HTTPException(422, f"Expected items list, got {type(raw_items).__name__}")

    # ── 早期校验：有 items 但 category 为空 → 立即拒绝，不创建空壳 submission ─────
    # 注：此时尚未创建 BidSubmission，确保 rollback 不留残留。
    _has_real_items = any(
        str(r.get("material") or "").strip() for r in raw_items if isinstance(r, dict)
    )
    if _has_real_items and not default_category:
        raise HTTPException(
            422,
            "category 不能为空：无法确定报价品类，入库中止。"
            "请在前端选择品类（如「阀门」）后重新点击「校对入库」。",
        )

    items: list[dict[str, Any]] = []
    shape_errors: list[dict] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            items.append(item)
        else:
            shape_errors.append({"row": idx + 1, "reason": f"not an object: {type(item).__name__}"})

    # ── 幂等：BidSubmission.batch_id 检查（一个 job 最多一条 BidSubmission）────────
    batch_id = f"BID-{job.id}"
    prior_submission = (
        db.query(BidSubmission).filter_by(batch_id=batch_id).first()
    )
    display_name = (
        body.supplier_name.strip()
        or (supplier.name if supplier else "")
        or (job.result or {}).get("supplier_name", "")
    )
    if prior_submission:
        # 同一文件 → 同一 job → 同一 batch_id。若历史那条已被 superseded/rejected
        # （旧轮次或修复脚本废弃），绝不能作为"幂等命中"原样返回——否则前端会拿到一个
        # 已废弃的 submission_id，下游 match 硬闸门必然 409（"不属于当前项目或已被废弃"）。
        # 正确语义：用户重新上传并再确认 = 复活该 submission（清旧行、重置 pending、重建）。
        _stale = prior_submission.status in ("superseded", "rejected")
        prior_line_count = db.query(BidQuoteLine).filter_by(
            submission_id=prior_submission.id
        ).count()
        if prior_line_count > 0 and not _stale:
            # 真正的幂等：活跃且已有报价行，直接返回
            log.info(
                "batch_confirm: idempotent hit, submission_id=%d batch=%s lines=%d",
                prior_submission.id, batch_id, prior_line_count,
            )
            return {
                "status": "ok",
                "submission_id": prior_submission.id,
                "line_count": prior_line_count,
                "skipped_count": 0,
                "errors": [],
                "unknown_brands": [],
                "supplier_id": prior_submission.supplier_id,
                "project_id": project.id if project else None,
                "batch_id": batch_id,
                "idempotent": True,
            }
        if _stale:
            # 复活废弃 submission：删除旧行后重建，并重置为 pending。
            deleted = db.query(BidQuoteLine).filter_by(
                submission_id=prior_submission.id
            ).delete()
            log.warning(
                "batch_confirm: reviving %s submission_id=%d batch=%s "
                "(cleared %d stale lines → pending)",
                prior_submission.status, prior_submission.id, batch_id, deleted,
            )
            prior_submission.status = "pending"
            # 复活时同步本次请求的归属（supplier_id/project 可能与废弃时不同）
            prior_submission.supplier_id = supplier.id if supplier else None
            if project:
                prior_submission.project_id = project.id
        else:
            # 空壳 submission（活跃但 0 行）：重建。复用现有对象，重新写 BQL。
            log.warning(
                "batch_confirm: rebuilding empty shell submission_id=%d batch=%s",
                prior_submission.id, batch_id,
            )
        # 更新供应商名称（可能本次传入了正确的 supplier_raw_name）
        if display_name:
            prior_submission.supplier_raw_name = display_name
        if body.bid_status:
            prior_submission.bid_status = body.bid_status
        submission = prior_submission
    else:
        # ── 创建 BidSubmission ─────────────────────────────────────────────────
        submission = BidSubmission(
            job_id=job.id,
            supplier_id=supplier.id if supplier else None,
            supplier_raw_name=display_name,
            project_id=project.id if project else None,
            batch_id=batch_id,
            status="pending",
            bid_status=body.bid_status,
        )
        db.add(submission)
        db.flush()

    if not items:
        db.commit()
        return {
            "status": "ok",
            "submission_id": submission.id,
            "line_count": 0,
            "skipped_count": 0,
            "errors": shape_errors,
            "unknown_brands": [],
            "supplier_id": submission.supplier_id,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
        }

    # ── 逐行处理 → BidQuoteLine（P0：Material 未找到时 material_id=NULL，不创建）──
    from apps.api.services.comparison import get_category_thresholds, determine_alert

    thresholds_cache: dict[str, dict] = {}
    line_count = 0
    skipped_count = 0
    errors: list[dict] = list(shape_errors)
    unknown_brands: set[str] = set()
    line_total_sum: float = 0.0

    for idx, item in enumerate(items):
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped_count += 1
                continue
            if _GRAND_TOTAL_NAME_RE.search(raw_name):
                log.info("batch_confirm: skipping aggregate row %r", raw_name)
                skipped_count += 1
                continue

            item_category = str(item.get("category") or "").strip() or default_category
            if not item_category or item_category not in PROFESSION_MAP:
                errors.append({"row": idx + 1, "reason": f"invalid category: {item_category!r}"})
                skipped_count += 1
                continue

            ai_std_name = str(item.get("standard_name") or "").strip()
            if ai_std_name:
                standard_name = ai_std_name
            else:
                standard_name = standardize_name(raw_name, item_category)["standardized"]

            spec = str(item.get("standard_spec") or item.get("spec") or "").strip()

            # Material 查找（P0：仅查找，不创建）
            mat: Material | None = None
            matched_mid = item.get("matched_material_id")
            if matched_mid is not None:
                try:
                    mat = db.get(Material, int(matched_mid))
                except (ValueError, TypeError):
                    pass
            if not mat:
                mat = (
                    db.query(Material)
                    .filter_by(category=item_category, standard_name=standard_name, spec=spec)
                    .first()
                )
            # 未找到 → material_id=NULL（不创建 Material，不报错）

            # 品牌等级
            brand = str(item.get("brand") or "").strip()
            brand_tier = ""
            if brand:
                bt = db.query(BrandTier).filter_by(brand_name=brand).first()
                if bt:
                    brand_tier = bt.tier
                else:
                    unknown_brands.add(brand)

            qty = float(q) if (q := item.get("qty")) is not None else None

            # ── 价格口径桥接（§4/§9）：判定 price_basis + effective 价格 ──────────────
            # batch-confirm 现场 re-derive，使前端编辑过的原始价格字段生效。
            # 不信任客户端回传的 price_basis（可能因人工编辑而陈旧）——一律以现场重算为准。
            basis_info = derive_price_basis(item)
            price_basis = basis_info["price_basis"]
            # 人工确认优先：前端"含税单价/总价(原文)"列编辑值落在 unit_price/total_price，
            # 若非空即视为人工确认，优先于自动 effective；否则采用桥接 effective。
            confirmed_unit = float(cu) if (cu := item.get("unit_price")) is not None else None
            confirmed_total = float(ct) if (ct := item.get("total_price")) is not None else None
            price = (
                confirmed_unit
                if confirmed_unit is not None
                else basis_info["effective_unit_price"]
            )
            total = (
                confirmed_total
                if confirmed_total is not None
                else basis_info["effective_total_price"]
            )
            # effective 合价缺失但有 effective 单价×数量 → 同口径相乘补全（非 ×1.13 推导）
            if total is None and price is not None and qty is not None:
                total = round(price * qty, 4)
            if total is not None:
                total = float(total)

            # 偏差计算（仅当 material 找到且有参考价时）
            deviation: float | None = None
            alert: str = ""
            if mat and price:
                ref = mat.ref_price_reasonable_low or mat.ref_price_median
                if ref and ref > 0:
                    if item_category not in thresholds_cache:
                        thresholds_cache[item_category] = get_category_thresholds(db, item_category)
                    deviation = round((price - ref) / ref, 4)
                    alert = determine_alert(deviation, thresholds_cache[item_category])

            extraction_meta = {
                "extraction_job_id": body.job_id,
                "source_ref": item.get("source_ref"),
                "raw_material": raw_name,
                "raw_spec": str(item.get("spec") or "").strip(),
                "raw_unit": str(item.get("unit") or "").strip(),
                "raw_remark": str(item.get("remark") or "").strip(),
                "material_type": str(item.get("material_type") or "").strip(),
                "canonical": item.get("canonical") or {},
                "validation_warning": item.get("validation_warning") or "",
                "normalized_material": str(item.get("normalized_material") or "").strip(),
                "ocr_correction_reason": str(item.get("ocr_correction_reason") or "").strip(),
                # ── 价格口径桥接审计（§4/§9）：basis + 全部原始税价字段，原值不改 ──
                "price_basis": price_basis,
                "effective_unit_price": basis_info["effective_unit_price"],
                "effective_total_price": basis_info["effective_total_price"],
                "effective_unit_recovered": basis_info.get("effective_unit_recovered", False),
                "raw_unit_price": _num_or_none(item.get("unit_price")),
                "raw_unit_price_incl_tax": _num_or_none(item.get("unit_price_incl_tax")),
                "raw_unit_price_excl_tax": _num_or_none(item.get("unit_price_excl_tax")),
                "raw_total_price": _num_or_none(item.get("total_price")),
                "raw_total_price_incl_tax": _num_or_none(item.get("total_price_incl_tax")),
                "raw_total_price_excl_tax": _num_or_none(item.get("total_price_excl_tax")),
                "tax_rate": _num_or_none(item.get("tax_rate")),
                "tax_amount": _num_or_none(item.get("tax_amount")),
                # 全局文档行序：顺序直连对齐的行身份（不依赖 source_ref.row / db id）。
                "document_row_index": (
                    int(v) if (v := item.get("document_row_index")) is not None else None
                ),
                # ── 算术校验审计：原 qty 不改，suggested_qty 仅参考 ──
                "validation_flags": list(item.get("validation_flags") or []),
                "raw_qty": _num_or_none(item.get("raw_qty")) if item.get("raw_qty") is not None else qty,
                "suggested_qty": _num_or_none(item.get("suggested_qty")),
            }

            line = BidQuoteLine(
                submission_id=submission.id,
                material_id=mat.id if mat else None,
                raw_name=raw_name,
                standard_name=standard_name,
                category=item_category,
                spec=spec,
                unit=str(item.get("unit") or ""),
                qty=qty,
                unit_price=price,
                unit_price_excl_tax=(
                    float(v) if (v := item.get("unit_price_excl_tax")) is not None else None
                ),
                tax_rate=(float(v) if (v := item.get("tax_rate")) is not None else None),
                total_price=total,
                brand=brand,
                brand_tier=brand_tier,
                remark=str(item.get("remark") or "")[:500],
                quote_date=str(item.get("quote_date") or ""),
                canonical=item.get("canonical"),
                extraction_meta=extraction_meta,
                deviation_pct=deviation,
                alert_level=alert,
            )
            db.add(line)
            line_count += 1
            if total is not None:
                line_total_sum += total

        except Exception as e:
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped_count += 1

    # ── 强校验：items 非空但全部被跳过 → 回滚并返回 422 ──────────────────────────
    # 这里捕获的主要情形是：category 虽然传到了后端，但每行的 item_category 仍无效。
    # 也防止空壳重建失败（重建后仍 0 行）时静默返回 ok。
    if items and line_count == 0:
        db.rollback()
        reason_summary = "; ".join({e["reason"] for e in errors[:3]}) if errors else "品类无效或所有行被过滤"
        raise HTTPException(
            422,
            f"所有 {len(items)} 行报价均被跳过，入库已回滚。原因：{reason_summary}",
        )

    db.commit()

    # ── checksum 回写到 job.result（供 bid_matrix 展示核查信息）────────────────
    try:
        doc_meta = (job.result or {}).get("_doc_meta") or {}
        declared = doc_meta.get("bid_total")
        if declared and float(declared) > 0 and line_count > 0:
            delta_pct = abs(line_total_sum - float(declared)) / float(declared) * 100
            cs_status = "pass" if delta_pct <= 5 else "fail"
        else:
            delta_pct = None
            cs_status = "unknown"
        job.result = {
            **(job.result or {}),
            "_checksum": {
                "declared": declared,
                "line_sum": round(line_total_sum, 2),
                "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
                "status": cs_status,
            },
        }
        ctx = dict(job.context or {})
        if submission.supplier_id and ctx.get("supplier_id") != submission.supplier_id:
            ctx["supplier_id"] = submission.supplier_id
            job.context = ctx
        db.add(job)
        db.commit()
    except Exception:
        log.exception("batch_confirm: checksum write failed for job %s", body.job_id)

    return {
        "status": "ok",
        "submission_id": submission.id,
        "line_count": line_count,
        "skipped_count": skipped_count,
        "errors": errors,
        "unknown_brands": sorted(unknown_brands),
        "supplier_id": submission.supplier_id,
        "project_id": project.id if project else None,
        "batch_id": batch_id,
    }


# ─── Archive prices: BidSubmission → Quote（显式归档）────────────────────────
class ArchivePricesRequest(BaseModel):
    """将 BidSubmission 中 material_id 非空的行归档为 Quote 历史价格记录。"""

    submission_id: int
    project_id: int | None = None  # 覆盖 BidSubmission.project_id（可选）


@router.post("/archive-prices", response_model=dict)
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

    lines = (
        db.query(BidQuoteLine).filter_by(submission_id=submission.id).all()
    )

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
