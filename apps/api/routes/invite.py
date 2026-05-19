"""Invite/邀标 routes — recommend suppliers and persist invitation lists.

Endpoints:
- POST /api/invite/recommend  — given tender items, return TOP-N supplier recos
- POST /api/invite/save       — create TenderDocument + BidInvitation rows
- GET  /api/invite/tenders    — list previously saved tenders (admin/debug)
- GET  /api/invite/tenders/{id} — fetch a saved tender + its invitations
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.models import BidInvitation, Supplier, TenderDocument
from apps.api.schemas.invite import (
    RecommendRequest,
    RecommendResponse,
    SaveInvitationsRequest,
    SaveInvitationsResponse,
    SavedInvitation,
    SupplierRecommendation,
)
from apps.api.services.supplier_recommend import (
    infer_categories,
    recommend_suppliers,
)

router = APIRouter(prefix="/api/invite", tags=["invite"])


@router.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest, db: Session = Depends(get_db)) -> RecommendResponse:
    categories = infer_categories(req.tender_items)
    recs = recommend_suppliers(
        db,
        req.tender_items,
        top_n=max(1, req.top_n),
        project_id=req.project_id,
    )
    return RecommendResponse(
        categories=categories,
        recommendations=[SupplierRecommendation(**r) for r in recs],
    )


@router.post("/save", response_model=SaveInvitationsResponse)
def save(req: SaveInvitationsRequest, db: Session = Depends(get_db)) -> SaveInvitationsResponse:
    if not req.supplier_ids:
        raise HTTPException(status_code=400, detail="supplier_ids cannot be empty")

    # Reuse existing tender or create new
    tender: TenderDocument | None = None
    if req.tender_id:
        tender = db.get(TenderDocument, req.tender_id)
        if not tender:
            raise HTTPException(status_code=404, detail=f"Tender {req.tender_id} not found")
    if tender is None:
        tender = TenderDocument(
            job_id=req.job_id,
            project_id=req.project_id,
            project_name=req.project_name or "",
            project_code=req.project_code or "",
            tender_date=req.tender_date or "",
            deadline=req.deadline or "",
            items=req.items or [],
            status="draft",
        )
        db.add(tender)
        db.flush()

    # Resolve recommendation context so saved rows carry rank/score
    rec_lookup: dict[int, dict] = {}
    if req.items:
        recs = recommend_suppliers(db, req.items, top_n=max(len(req.supplier_ids), 5))
        rec_lookup = {r["supplier_id"]: r for r in recs}

    saved: list[SavedInvitation] = []
    for sid in req.supplier_ids:
        sup = db.get(Supplier, sid)
        if not sup:
            continue
        # Skip duplicates within same tender
        existing = db.query(BidInvitation).filter_by(
            tender_id=tender.id, supplier_id=sid
        ).first()
        if existing:
            inv = existing
        else:
            r = rec_lookup.get(sid)
            inv = BidInvitation(
                tender_id=tender.id,
                supplier_id=sid,
                score=r["score"] if r else None,
                rank=r["rank"] if r else None,
                reason=r["reason"] if r else {},
                status="pending",
            )
            db.add(inv)
            db.flush()
        saved.append(SavedInvitation(
            id=inv.id,
            supplier_id=sid,
            supplier_name=sup.name,
            rank=inv.rank,
            score=inv.score,
            status=inv.status,
        ))

    # Tender becomes 'invited' once at least one supplier is saved
    if saved and tender.status == "draft":
        tender.status = "invited"

    db.commit()
    return SaveInvitationsResponse(tender_id=tender.id, invitations=saved)


@router.get("/tenders", response_model=list[dict])
def list_tenders(limit: int = 50, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(TenderDocument)
        .order_by(TenderDocument.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": t.id,
            "project_name": t.project_name,
            "project_code": t.project_code,
            "tender_date": t.tender_date,
            "deadline": t.deadline,
            "status": t.status,
            "item_count": len(t.items or []),
            "invitation_count": len(t.invitations),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


@router.get("/tenders/{tender_id}", response_model=dict)
def get_tender(tender_id: int, db: Session = Depends(get_db)) -> dict:
    t = db.get(TenderDocument, tender_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"Tender {tender_id} not found")
    invitations = [
        {
            "id": inv.id,
            "supplier_id": inv.supplier_id,
            "supplier_name": inv.supplier.name if inv.supplier else "",
            "rank": inv.rank,
            "score": inv.score,
            "reason": inv.reason,
            "status": inv.status,
        }
        for inv in t.invitations
    ]
    return {
        "id": t.id,
        "project_name": t.project_name,
        "project_code": t.project_code,
        "tender_date": t.tender_date,
        "deadline": t.deadline,
        "items": t.items,
        "status": t.status,
        "invitations": invitations,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }
