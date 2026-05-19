"""Quote CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from apps.api.core.database import get_db
from apps.api.models import Quote, Material
from apps.api.schemas import QuoteCreate, QuoteUpdate, QuoteOut, ImportResult
from apps.api.services.import_service import import_csv_data

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


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
