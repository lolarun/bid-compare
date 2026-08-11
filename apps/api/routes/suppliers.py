"""Supplier CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
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
    merge_status: str = Query("active", description="active|merged|inactive|all"),
    db: Session = Depends(get_db),
):
    stmt = select(Supplier)
    if merge_status != "all":
        stmt = stmt.where(Supplier.merge_status == merge_status)
    if keyword:
        stmt = stmt.where(Supplier.name.contains(keyword) | Supplier.short_name.contains(keyword))
    if category:
        stmt = stmt.where(Supplier.categories.contains(f'"{category}"'))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Supplier.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [SupplierOut.model_validate(i).model_dump() for i in items],
    }


@router.get("/search", response_model=list)
def search_suppliers(
    q: str = Query(..., min_length=1, description="供应商名称（支持别名/模糊搜索）"),
    limit: int = Query(10, ge=1, le=50),
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
):
    """供应商名称搜索（用于 batch-confirm 时的供应商选择下拉）。

    支持 Supplier.name / short_name 模糊匹配 + SupplierAlias 精确规范化匹配。
    """
    from apps.api.services.supplier_resolve import search_suppliers_by_name
    return search_suppliers_by_name(db, q, limit=limit, active_only=active_only)


@router.get("/resolve", response_model=dict)
def resolve_supplier_by_name(
    name: str = Query(..., min_length=1, description="供应商原始名称（来自 OCR 或文件名）"),
    db: Session = Depends(get_db),
):
    """7层供应商解析 — 将原始名称映射到 canonical supplier_id。

    返回：
      {matched: true, supplier: {...}}          精确命中一个
      {matched: false, ambiguous: true, candidates: [...]}  歧义（多个候选）
      {matched: false, ambiguous: false}        未找到
    """
    from apps.api.services.supplier_resolve import resolve_supplier
    result = resolve_supplier(db, name)
    if result.supplier:
        sup = result.supplier
        return {
            "matched": True,
            "layer": result.matched_layer,
            "supplier": {"id": sup.id, "name": sup.name, "short_name": sup.short_name or ""},
        }
    if result.candidates:
        return {
            "matched": False,
            "ambiguous": True,
            "candidates": result.candidates,
            "normalized": result.normalized,
        }
    return {"matched": False, "ambiguous": False, "normalized": result.normalized}


@router.get("/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: int, db: Session = Depends(get_db)):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise HTTPException(404, "Supplier not found")
    return sup


@router.post("", response_model=SupplierOut, status_code=201)
def create_supplier(body: SupplierCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(Supplier).where(Supplier.name == body.name))
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

    quote_count = db.scalar(select(func.count(Quote.id)).where(Quote.supplier_id == supplier_id)) or 0
    invitation_count = db.scalar(select(func.count(BidInvitation.id)).where(BidInvitation.supplier_id == supplier_id)) or 0

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
