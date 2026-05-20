"""Supplier CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models import BidInvitation, Quote, Supplier
from apps.api.schemas import SupplierCreate, SupplierUpdate, SupplierOut

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


@router.get("", response_model=dict)
def list_suppliers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(Supplier)
    if keyword:
        q = q.filter(Supplier.name.contains(keyword) | Supplier.short_name.contains(keyword))
    if category:
        q = q.filter(Supplier.categories.contains(f'"{category}"'))

    total = q.count()
    items = q.order_by(Supplier.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [SupplierOut.model_validate(i).model_dump() for i in items],
    }


@router.get("/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: int, db: Session = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")
    return sup


@router.post("", response_model=SupplierOut, status_code=201)
def create_supplier(body: SupplierCreate, db: Session = Depends(get_db)):
    existing = db.query(Supplier).filter(Supplier.name == body.name).first()
    if existing:
        raise HTTPException(409, f"Supplier '{body.name}' already exists")

    sup = Supplier(**body.model_dump())
    db.add(sup)
    db.commit()
    db.refresh(sup)
    return sup


@router.put("/{supplier_id}", response_model=SupplierOut)
def update_supplier(supplier_id: int, body: SupplierUpdate, db: Session = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sup, field, value)

    db.commit()
    db.refresh(sup)
    return sup


@router.delete("/{supplier_id}", status_code=204)
def delete_supplier(supplier_id: int, db: Session = Depends(get_db)):
    """Soft-delete via referential-integrity guard.

    Policy (业务决策, 2026-05-20): suppliers are NEVER physically deleted
    once they carry historical data — quotes/invitations would either
    cascade away or become orphans, both unacceptable for audit trails.

    To "remove" a supplier from active use, edit it (set status / rename),
    don't delete. We return 409 Conflict with the reference counts so the
    UI can show a sensible error.
    """
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")

    quote_count = db.query(func.count(Quote.id)).filter(
        Quote.supplier_id == supplier_id
    ).scalar() or 0
    invitation_count = db.query(func.count(BidInvitation.id)).filter(
        BidInvitation.supplier_id == supplier_id
    ).scalar() or 0

    if quote_count > 0 or invitation_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Supplier '{sup.name}' cannot be deleted: it is "
                    f"referenced by {quote_count} quote(s) and "
                    f"{invitation_count} invitation(s). "
                    "Delete or reassign these records first, OR edit the "
                    "supplier instead of deleting it."
                ),
                "quote_count": quote_count,
                "invitation_count": invitation_count,
            },
        )

    db.delete(sup)
    db.commit()
