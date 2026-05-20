"""Analysis and comparison API endpoints — v2."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.schemas import (
    PriceCompareRequest, PriceCompareResult,
    SupplierScoreRequest, SupplierScoreResult,
    DashboardSummary,
    MultiCompareRequest, MultiCompareResult,
    CategoryDetailStats,
    BidMatrixRequest, BidMatrixResult,
    DashboardHeatmapData, DashboardBubbleData,
)
from apps.api.services.comparison import compare_price
from apps.api.services.scoring import score_supplier, compare_multiple_suppliers
from apps.api.services.statistics import (
    get_dashboard_summary,
    get_category_detail_stats,
    refresh_material_baselines,
    get_dashboard_heatmap,
    get_dashboard_bubble,
)
from apps.api.services.bid_matrix import build_bid_matrix

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.post("/compare", response_model=PriceCompareResult)
def price_compare(body: PriceCompareRequest, db: Session = Depends(get_db)):
    result = compare_price(
        db,
        category=body.category,
        sub_category=body.sub_category,
        new_price=body.new_price,
    )
    return result


@router.post("/supplier-score", response_model=SupplierScoreResult)
def supplier_score(body: SupplierScoreRequest, db: Session = Depends(get_db)):
    try:
        result = score_supplier(db, body.supplier_id, body.category, weights=body.weights)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


@router.post("/multi-compare", response_model=MultiCompareResult)
def multi_compare(body: MultiCompareRequest, db: Session = Depends(get_db)):
    result = compare_multiple_suppliers(
        db,
        supplier_ids=body.supplier_ids,
        category=body.category,
        project_id=body.project_id,
        weights=body.weights,
    )
    return result


@router.post("/bid-matrix", response_model=BidMatrixResult)
def bid_matrix(body: BidMatrixRequest, db: Session = Depends(get_db)):
    """横向对比矩阵 — F6.1 核心接口。"""
    result = build_bid_matrix(
        db,
        supplier_ids=body.supplier_ids,
        project_id=body.project_id,
        material_ids=body.material_ids,
        category=body.category,
    )
    return result


@router.get("/category-stats/{category}", response_model=CategoryDetailStats)
def category_stats(category: str, db: Session = Depends(get_db)):
    result = get_category_detail_stats(db, category)
    if result["total_records"] == 0:
        raise HTTPException(404, f"No data for category '{category}'")
    return result


@router.get("/dashboard", response_model=DashboardSummary)
def dashboard(db: Session = Depends(get_db)):
    return get_dashboard_summary(db)


@router.get("/dashboard/heatmap", response_model=DashboardHeatmapData)
def dashboard_heatmap(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """树状热力图数据：项目 → 品类 → 采购金额。"""
    return get_dashboard_heatmap(db, date_from, date_to)


@router.get("/dashboard/bubble", response_model=DashboardBubbleData)
def dashboard_bubble(db: Session = Depends(get_db)):
    """气泡图数据：品类 → 供应商 → 采购金额。"""
    return get_dashboard_bubble(db)


@router.post("/refresh-baselines")
def refresh_baselines(category: str | None = None, db: Session = Depends(get_db)):
    refresh_material_baselines(db, category)
    return {"status": "ok", "message": f"Baselines refreshed for {category or 'all categories'}"}
