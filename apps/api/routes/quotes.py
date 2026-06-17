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
)
from apps.api.schemas import QuoteCreate, QuoteUpdate, QuoteOut, ImportResult
from apps.api.services.import_service import import_csv_data, _gen_code
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
    def _fuzzy_supplier_candidates(db, name: str, threshold: float = 0.75) -> list[dict]:
        """返回相似度超过 threshold 的已有供应商列表，用于人工确认去重。"""
        from difflib import SequenceMatcher
        all_sups = db.query(Supplier).all()
        candidates = []
        for s in all_sups:
            ratio = SequenceMatcher(None, name, s.name).ratio()
            if ratio >= threshold:
                candidates.append({"id": s.id, "name": s.name, "similarity": round(ratio, 3)})
        return sorted(candidates, key=lambda x: -x["similarity"])

    supplier: Supplier | None = None
    if body.supplier_id:
        supplier = db.get(Supplier, body.supplier_id)
        if not supplier:
            raise HTTPException(404, f"Supplier {body.supplier_id} not found")
    elif body.supplier_name.strip():
        name = body.supplier_name.strip()
        supplier = db.query(Supplier).filter_by(name=name).first()
        if not supplier:
            # 模糊去重：相似度≥0.75 时要求人工确认，禁止静默新建
            candidates = _fuzzy_supplier_candidates(db, name)
            if candidates:
                raise HTTPException(409, {
                    "error": "supplier_alias_conflict",
                    "message": f"供应商「{name}」与已有记录高度相似，请确认是否为同一家",
                    "input_name": name,
                    "candidates": candidates[:5],
                })
            supplier = Supplier(name=name)
            db.add(supplier)
            db.flush()
    else:
        # Try from job result or context
        sname = (job.result or {}).get("supplier_name") or (job.context or {}).get("supplier_name")
        if sname:
            supplier = db.query(Supplier).filter_by(name=sname).first()
            if not supplier:
                candidates = _fuzzy_supplier_candidates(db, sname)
                if candidates:
                    raise HTTPException(409, {
                        "error": "supplier_alias_conflict",
                        "message": f"OCR 识别供应商「{sname}」与已有记录高度相似，请确认后再入库",
                        "input_name": sname,
                        "candidates": candidates[:5],
                    })
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
    # Category can now come from three sources (highest to lowest priority):
    #   1. Per-item "category" field (from AI enhance step)
    #   2. Job context "category"
    #   3. Top-level body.category
    # If all items carry their own category, the top-level is optional.
    default_category = (
        body.category.strip()
        or (job.context or {}).get("category", "")
        or ""
    )
    # Validate default if provided (but don't require it)
    if default_category and default_category not in PROFESSION_MAP:
        raise HTTPException(400, f"Unknown category: {default_category}")

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

    thresholds_cache: dict[str, dict] = {}  # cache per category
    created = 0
    skipped = 0
    errors: list[dict] = list(shape_errors)
    unknown_brands: set[str] = set()
    quote_ids: list[int] = []
    line_total_sum: float = 0.0

    for idx, item in enumerate(items):
        try:
            raw_name = str(item.get("material") or "").strip()
            if not raw_name:
                skipped += 1
                continue

            # Block grand_total/subtotal rows from entering DB as regular quotes
            if _GRAND_TOTAL_NAME_RE.search(raw_name):
                log.info("batch_confirm: skipping aggregate row '%s'", raw_name)
                skipped += 1
                continue

            # Per-item category: item.category > default_category
            item_category = str(item.get("category") or "").strip() or default_category
            if not item_category or item_category not in PROFESSION_MAP:
                errors.append({"row": idx + 1, "reason": f"Missing or invalid category: '{item_category}'"})
                skipped += 1
                continue
            item_profession = PROFESSION_MAP[item_category]

            # Use AI standard_name if provided, else rule-based standardization
            ai_standard_name = str(item.get("standard_name") or "").strip()
            if ai_standard_name:
                standard_name = ai_standard_name
            else:
                std_result = standardize_name(raw_name, item_category)
                standard_name = std_result["standardized"]

            # Use AI standard_spec if provided, else original spec
            spec = str(item.get("standard_spec") or item.get("spec") or "").strip()

            # Try matched_material_id first (from AI enhance)
            mat = None
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
            if not mat:
                mat = Material(
                    material_code=_gen_code(db, item_profession, item_category),
                    standard_name=standard_name,
                    profession=item_profession,
                    category=item_category,
                    sub_category="",
                    spec=spec,
                    material_type=str(item.get("material_type") or "").strip(),
                    unit=str(item.get("unit") or ""),
                    brand=str(item.get("brand") or ""),
                )
                db.add(mat)
                db.flush()

            # Persist canonical, validation_warning, and OCR-correction fields
            canonical = item.get("canonical")
            validation_warning = item.get("validation_warning") or ""
            norm_mat = str(item.get("normalized_material") or "").strip()
            ocr_reason = str(item.get("ocr_correction_reason") or "").strip()
            if canonical or validation_warning or norm_mat or ocr_reason:
                ext = dict(mat.extended_attrs or {})
                if canonical and "canonical" not in ext:
                    ext["canonical"] = canonical
                if validation_warning:
                    ext["validation_warning"] = validation_warning
                if norm_mat and "normalized_material" not in ext:
                    ext["normalized_material"] = norm_mat
                if ocr_reason and "ocr_correction_reason" not in ext:
                    ext["ocr_correction_reason"] = ocr_reason
                mat.extended_attrs = ext

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

            # Deviation + alert (cache thresholds per category)
            if item_category not in thresholds_cache:
                thresholds_cache[item_category] = get_category_thresholds(db, item_category)
            thresholds = thresholds_cache[item_category]
            ref = mat.ref_price_reasonable_low or mat.ref_price_median
            deviation = None
            alert = ""
            if price and ref and ref > 0:
                deviation = round((price - ref) / ref, 4)
                alert = determine_alert(deviation, thresholds)

            # Row-level extraction evidence: preserve the supplier's ORIGINAL
            # expression (pre-standardization) so the LLM supplier-fill agent can
            # judge "like a human reading the quote" rather than the normalized name.
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
            }

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
                bid_status=body.bid_status,
                deviation_pct=deviation,
                alert_level=alert,
                extraction_meta_json=extraction_meta,
            )
            db.add(q)
            db.flush()
            quote_ids.append(q.id)
            created += 1
            if total is not None:
                line_total_sum += total
        except Exception as e:  # pragma: no cover — per-row resilience
            errors.append({"row": idx + 1, "reason": f"{type(e).__name__}: {e}"})
            skipped += 1

    db.commit()

    # Compute checksum: declared total (from PDF cover) vs sum of created quote lines.
    # Stored in job.result["_checksum"] so bid_matrix can flag OCR-unreliable suppliers.
    try:
        doc_meta = (job.result or {}).get("_doc_meta") or {}
        declared = doc_meta.get("bid_total")
        if declared and float(declared) > 0 and created > 0:
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
        db.add(job)
        db.commit()
    except Exception:
        log.exception("batch_confirm: checksum calculation failed for job %s", body.job_id)

    # Write supplier_id back to ExtractionJob.context so doc_meta lookup can find it
    if supplier:
        ctx = dict(job.context or {})
        if ctx.get("supplier_id") != supplier.id:
            ctx["supplier_id"] = supplier.id
            job.context = ctx
            db.add(job)
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
