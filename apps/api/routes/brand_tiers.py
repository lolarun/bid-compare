"""Brand tier CRUD API endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models import BrandTier, Quote
from apps.api.schemas import BrandTierCreate, BrandTierUpdate, BrandTierOut

router = APIRouter(prefix="/api/brand-tiers", tags=["brand-tiers"])


@router.get("", response_model=list[BrandTierOut])
def list_brand_tiers(
    category: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(BrandTier)
    if category:
        stmt = stmt.where((BrandTier.category == category) | BrandTier.category.is_(None))
    return db.scalars(stmt.order_by(BrandTier.brand_name)).all()


@router.get("/unknown", response_model=list[str])
def list_unknown_brands(
    category: str | None = None,
    db: Session = Depends(get_db),
):
    """Return brand names that appear in quotes but have no tier mapping."""
    from apps.api.models import Material

    stmt = select(Quote.brand).where(
        Quote.brand != "",
        Quote.brand.isnot(None),
    )
    if category:
        stmt = stmt.join(Material).where(Material.category == category)
    all_brands = {brand for brand in db.scalars(stmt.distinct()).all() if brand}

    tiered_brands = set(db.scalars(select(BrandTier.brand_name)).all())
    unknown = sorted(all_brands - tiered_brands)
    return unknown


@router.get("/{tier_id}", response_model=BrandTierOut)
def get_brand_tier(tier_id: int, db: Session = Depends(get_db)):
    bt = db.get(BrandTier, tier_id)
    if not bt:
        raise HTTPException(404, "Brand tier not found")
    return bt


@router.post("", response_model=BrandTierOut, status_code=201)
def create_brand_tier(body: BrandTierCreate, db: Session = Depends(get_db)):
    # Upsert: if (brand_name, category) exists, update tier
    existing = db.scalar(select(BrandTier).where(
        BrandTier.brand_name == body.brand_name,
        BrandTier.category == body.category,
    ))
    if existing:
        existing.tier = body.tier
        db.commit()
        db.refresh(existing)
        return existing

    bt = BrandTier(**body.model_dump())
    db.add(bt)
    db.commit()
    db.refresh(bt)
    return bt


@router.put("/{tier_id}", response_model=BrandTierOut)
def update_brand_tier(tier_id: int, body: BrandTierUpdate, db: Session = Depends(get_db)):
    bt = db.get(BrandTier, tier_id)
    if not bt:
        raise HTTPException(404, "Brand tier not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(bt, field, value)
    db.commit()
    db.refresh(bt)
    return bt


@router.delete("/{tier_id}", status_code=204)
def delete_brand_tier(tier_id: int, db: Session = Depends(get_db)):
    bt = db.get(BrandTier, tier_id)
    if not bt:
        raise HTTPException(404, "Brand tier not found")
    db.delete(bt)
    db.commit()
