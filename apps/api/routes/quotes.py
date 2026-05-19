"""Quote CRUD API endpoints."""

import logging
from typing import Any

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
)
from apps.api.schemas import QuoteCreate, QuoteUpdate, QuoteOut, ImportResult
from apps.api.services.import_service import import_csv_data
from apps.api.services.standardize import standardize_name

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


class BatchConfirmRequest(BaseModel):
    """Materialise a DONE extraction job's items into Quote records."""

    job_id: str
    supplier_id: int | None = None
    supplier_name: str = ""  # used to create a new supplier if no id provided
    project_id: int | None = None
    project_name: str = ""
    category: str = ""  # required if items don't carry their own
    overrides: list[dict[str, Any]] | None = None  # user-edited items, if any


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
    db: Session = Depends(get_db),
):
    """Import a CSV or Excel file, creating Material + Quote records."""
    if not file.filename:
        raise HTTPException(400, "No file provided")
    if not file.filename.endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(400, "Only .csv, .xlsx, .xls files are supported")

    content = await file.read()
    result = import_csv_data(db, content, file.filename, category, project_name)
    if result["status"] == "error" and result["imported"] == 0:
        raise HTTPException(422, detail=result)
    return result


# ─── Batch confirm: convert ExtractionJob.result → Quote rows ──────────────
@router.post("/batch-confirm", response_model=dict)
def batch_confirm(body: BatchConfirmRequest = Body(...), db: Session = Depends(get_db)):
    """Materialise an extracted quote job into Material + Quote DB records.

    Flow:
    - Look up job; must be DONE and type=quote.
    - Resolve supplier (by id, or by name → get-or-create).
    - Resolve project (by id, or by name → get-or-create).
    - For each item (either job.result.items or `overrides` if provided):
        - Standardise material name
        - Get or create Material (by category, standard_name, spec)
        - Compute deviation + alert_level vs ref_price_reasonable_low/median
        - Create Quote linked to material+supplier+project
        - Collect unknown brands (no entry in brand_tiers table)
    - Returns {created, skipped, errors, unknown_brands, quote_ids}
    """
    job = db.get(ExtractionJob, body.job_id)
    if not job:
        raise HTTPException(404, f"Job {body.job_id} not found")
    if job.type != "quote":
        raise HTTPException(400, f"Job type is {job.type}; must be 'quote'")
    if job.status != "done":
        raise HTTPException(400, f"Job status is {job.status}; must be 'done'")

    # ── Resolve supplier ────────────────────────────────────────────────────
    supplier: Supplier | None = None
    if body.supplier_id:
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier:
            raise HTTPException(404, f"Supplier {body.supplier_id} not found")
    elif body.supplier_name.strip():
        name = body.supplier_name.strip()
        supplier = db.query(Supplier).filter_by(name=name).first()
        if not supplier:
            supplier = Supplier(name=name)
            db.add(supplier)
            db.flush()
    else:
        # Try from job result or context
        sname = (job.result or {}).get("supplier_name") or (job.context or {}).get("supplier_name")
        if sname:
            supplier = db.query(Supplier).filter_by(name=sname).first()
            if not supplier:
                supplier = Supplier(name=sname)
                db.add(supplier)
                db.flush()

    # ── Resolve project ────────────────────────────────────────────────────
    # AUDIT-FIX M2: when a project_id comes through (body or job context),
    # missing-target should be a 400, NOT silently null. Otherwise the user
    # uploads with a specific project and the quote lands unattached.
    project: Project | None = None
    if body.project_id:
        project = db.get(Project, body.project_id)
        if not project:
            raise HTTPException(404, f"Project {body.project_id} not found")
    elif body.project_name.strip():
        name = body.project_name.strip()
        project = db.query(Project).filter_by(name=name).first()
        if not project:
            project = Project(name=name)
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

    # ── Determine category ─────────────────────────────────────────────────
    category = (
        body.category.strip()
        or (job.context or {}).get("category", "")
        or ""
    )
    if not category:
        raise HTTPException(
            400, "Category required (provide `category` or set in job.context)"
        )
    if category not in PROFESSION_MAP:
        raise HTTPException(400, f"Unknown category: {category}")
    profession = PROFESSION_MAP[category]

    # ── Resolve item list (validate shape) ─────────────────────────────────
    raw_items: Any = (
        body.overrides
        if body.overrides is not None
        else (job.result or {}).get("items")
    )
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise HTTPException(
            422, detail=f"Expected items to be a list, got {type(raw_items).__name__}"
        )
    items: list[dict[str, Any]] = []
    shape_errors: list[dict] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            items.append(item)
        else:
            shape_errors.append({
                "row": idx + 1,
                "reason": f"row is not an object: {type(item).__name__}",
            })

    # ── Idempotency: batch_id derived from (job_id, supplier_id) so a
    # double-click on Confirm cannot create duplicate Quote rows. ────────────
    batch_id = f"OCR-{job.id[:8]}-{supplier.id if supplier else 'nos'}"
    prior = (
        db.query(Quote)
        .filter(Quote.batch_id == batch_id)
        .order_by(Quote.id.asc())
        .all()
    )
    if prior:
        log.info(
            "batch_confirm: idempotent hit, returning %d prior quotes for batch %s",
            len(prior),
            batch_id,
        )
        return {
            "status": "ok",
            "created": 0,
            "skipped": len(items),
            "errors": shape_errors,
            "unknown_brands": [],
            "quote_ids": [q.id for q in prior],
            "supplier_id": supplier.id if supplier else None,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
            "idempotent": True,
        }

    if not items:
        return {
            "status": "ok",
            "created": 0,
            "skipped": 0,
            "errors": shape_errors,
            "unknown_brands": [],
            "quote_ids": [],
            "supplier_id": supplier.id if supplier else None,
            "project_id": project.id if project else None,
            "batch_id": batch_id,
        }

    # ── Iterate & create ───────────────────────────────────────────────────
    from apps.api.services.comparison import get_category_thresholds, determine_alert

    thresholds = get_category_thresholds(db, category)
    created = 0
    skipped = 0
    errors: list[dict] = list(shape_errors)
    unknown_brands: set[str] = set()
    quote_ids: list[int] = []

    for idx, item in enumerate(items):
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped += 1
                continue
            std_result = standardize_name(raw_name, category)
            standard_name = std_result["standardized"]
            spec = str(item.get("spec") or "").strip()

            mat = (
                db.query(Material)
                .filter_by(category=category, standard_name=standard_name, spec=spec)
                .first()
            )
            if not mat:
                mat = Material(
                    standard_name=standard_name,
                    profession=profession,
                    category=category,
                    sub_category="",
                    spec=spec,
                    material_type="",
                    unit=str(item.get("unit") or ""),
                    brand=str(item.get("brand") or ""),
                )
                db.add(mat)
                db.flush()

            # Brand-tier lookup (track unknowns)
            brand = str(item.get("brand") or "").strip()
            brand_tier = ""
            if brand:
                bt = (
                    db.query(BrandTier)
                    .filter(BrandTier.brand_name == brand)
                    .first()
                )
                if bt:
                    brand_tier = bt.tier
                else:
                    unknown_brands.add(brand)

            price = item.get("unit_price")
            price = float(price) if price is not None else None
            qty = item.get("qty")
            qty = float(qty) if qty is not None else None
            total = item.get("total_price")
            if total is None and price is not None and qty is not None:
                total = round(price * qty, 4)

            # Deviation + alert
            ref = mat.ref_price_reasonable_low or mat.ref_price_median
            deviation = None
            alert = ""
            if price and ref and ref > 0:
                deviation = round((price - ref) / ref, 4)
                alert = determine_alert(deviation, thresholds)

            q = Quote(
                material_id=mat.id,
                supplier_id=supplier.id if supplier else None,
                project_id=project.id if project else None,
                unit_price=price,
                unit_price_excl_tax=item.get("unit_price_excl_tax"),
                quantity=qty,
                total_price=total,
                tax_rate=item.get("tax_rate"),
                brand=brand,
                brand_tier=brand_tier,
                remark=str(item.get("remark") or "")[:500],
                quote_date=str(item.get("quote_date") or ""),
                batch_id=batch_id,
                deviation_pct=deviation,
                alert_level=alert,
            )
            db.add(q)
            db.flush()
            quote_ids.append(q.id)
            created += 1
        except Exception as e:  # pragma: no cover — per-row resilience
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped += 1

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "skipped": skipped,
        "errors": errors,
        "unknown_brands": sorted(unknown_brands),
        "quote_ids": quote_ids,
        "supplier_id": supplier.id if supplier else None,
        "project_id": project.id if project else None,
        "batch_id": batch_id,
    }
