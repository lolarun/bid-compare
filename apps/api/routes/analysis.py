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
    BidInsightRequest, BidInsightResult,
    DashboardHeatmapData, DashboardBubbleData,
    AlignmentSuggestRequest, AlignmentSuggestResult,
    AlignmentApplyRequest, AlignmentApplyResult, AlignmentGroupOut,
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
from apps.api.services.bid_insight import generate_bid_insight
from apps.api.core.config import get_settings

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


@router.post("/bid-matrix")
def bid_matrix(body: BidMatrixRequest, db: Session = Depends(get_db)) -> BidMatrixResult:
    """横向对比矩阵 — F6.1 核心接口。"""
    result = build_bid_matrix(
        db,
        supplier_ids=body.supplier_ids,
        project_id=body.project_id,
        material_ids=body.material_ids,
        category=body.category,
    )
    return BidMatrixResult.model_validate(result)


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
def dashboard_bubble(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """气泡图数据：品类 → 供应商 → 采购金额。"""
    return get_dashboard_bubble(db, date_from, date_to)


@router.post("/refresh-baselines")
def refresh_baselines(category: str | None = None, db: Session = Depends(get_db)):
    refresh_material_baselines(db, category)
    return {"status": "ok", "message": f"Baselines refreshed for {category or 'all categories'}"}


@router.post("/bid-insight", response_model=BidInsightResult)
def bid_insight(body: BidInsightRequest):
    """AI 综合分析建议 — 调用 Qwen 文本模型分析比价矩阵。"""
    from openai import OpenAI

    _settings = get_settings()
    api_key = _settings.DASHSCOPE_API_KEY
    base_url = _settings.DASHSCOPE_BASE_URL
    if not api_key:
        return BidInsightResult(error="LLM API key not configured")

    client = OpenAI(api_key=api_key, base_url=base_url)
    matrix_data = body.model_dump()
    result = generate_bid_insight(matrix_data, client, model="qwen-plus")
    return result


@router.post("/bid-alignment/suggest", response_model=AlignmentSuggestResult)
def bid_alignment_suggest(body: AlignmentSuggestRequest, db: Session = Depends(get_db)):
    """AI 报价对齐复核 — 分析多供应商报价行，建议对齐分组和字段纠错。

    Two modes:
      1. Pass `rows` directly (from OCR results before DB import)
      2. Pass `project_id + supplier_ids + category` (query confirmed quotes from DB)
    """
    from openai import OpenAI
    from apps.api.services.bid_alignment import suggest_alignment
    from apps.api.models import Quote, Material, Supplier

    _settings = get_settings()
    api_key = _settings.DASHSCOPE_API_KEY
    base_url = _settings.DASHSCOPE_BASE_URL
    if not api_key:
        return AlignmentSuggestResult(error="LLM API key not configured")

    rows_data: list[dict] = []
    supplier_names: list[str] = []

    if body.rows:
        # Mode 1: rows passed directly
        rows_data = [r.model_dump() for r in body.rows]
        supplier_names = sorted(set(r.supplier_name for r in body.rows if r.supplier_name))
    elif body.supplier_ids:
        # Mode 2: query from DB
        q = db.query(Quote, Material, Supplier).join(
            Material, Quote.material_id == Material.id
        ).join(
            Supplier, Quote.supplier_id == Supplier.id
        ).filter(
            Quote.supplier_id.in_(body.supplier_ids),
            Quote.unit_price > 0,
        )
        if body.project_id:
            q = q.filter(Quote.project_id == body.project_id)
        if body.category:
            q = q.filter(Material.category == body.category)
        results = q.order_by(Material.standard_name, Supplier.name).limit(60).all()
        if not results:
            return AlignmentSuggestResult(error="No quote data found for given parameters")
        for qt, mat, sup in results:
            rows_data.append({
                "quote_id": qt.id,
                "supplier_id": qt.supplier_id,
                "supplier_name": sup.name,
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "unit": mat.unit or "",
                "quantity": qt.quantity,
                "unit_price": qt.unit_price,
                "total_price": qt.total_price,
            })
        supplier_names = sorted(set(r["supplier_name"] for r in rows_data if r.get("supplier_name")))
    else:
        return AlignmentSuggestResult(error="No quote rows or supplier_ids provided")

    client = OpenAI(api_key=api_key, base_url=base_url)
    result = suggest_alignment(
        rows=rows_data,
        category=body.category,
        supplier_names=supplier_names,
        client=client,
        model="qwen-plus",
    )
    return result


@router.post("/bid-alignment/apply", response_model=AlignmentApplyResult)
def bid_alignment_apply(body: AlignmentApplyRequest, db: Session = Depends(get_db)):
    """用户确认 AI 对齐建议 — 持久化分组并可选地修正字段。"""
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem

    groups_saved = 0
    items_saved = 0

    for g in body.groups:
        if g.status == "rejected":
            continue
        group = BidAlignmentGroup(
            project_id=body.project_id,
            category=body.category,
            suggested_name=g.suggested_name,
            suggested_spec=g.suggested_spec,
            suggested_unit=g.suggested_unit,
            suggested_qty=g.suggested_qty,
            confidence=g.confidence,
            reason=g.reason,
            status=g.status,
        )
        db.add(group)
        db.flush()  # get group.id
        for item in g.items:
            ai = BidAlignmentItem(
                group_id=group.id,
                quote_id=item.quote_id,
                supplier_id=item.supplier_id,
                action=item.action,
                spec_note=item.spec_note,
                name_note=item.name_note,
            )
            db.add(ai)
            items_saved += 1
        groups_saved += 1

    # Apply field fixes to quotes (e.g. correct unit_price ↔ total_price)
    fixes_applied = 0
    for fix in body.field_fixes:
        if fix.new_value is None:
            continue
        from apps.api.models.quote import Quote
        quote = db.query(Quote).get(fix.quote_id)
        if quote and fix.field == "unit_price":
            quote.unit_price = fix.new_value
            fixes_applied += 1

    db.commit()
    return AlignmentApplyResult(
        groups_saved=groups_saved,
        items_saved=items_saved,
        fixes_applied=fixes_applied,
    )


@router.get("/bid-alignment/groups", response_model=list[AlignmentGroupOut])
def bid_alignment_groups(
    project_id: int | None = Query(None),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """获取已确认的对齐分组。"""
    from apps.api.models.bid_alignment import BidAlignmentGroup
    q = db.query(BidAlignmentGroup)
    if project_id is not None:
        q = q.filter(BidAlignmentGroup.project_id == project_id)
    if category:
        q = q.filter(BidAlignmentGroup.category == category)
    q = q.filter(BidAlignmentGroup.status == "confirmed")
    groups = q.all()

    result = []
    for g in groups:
        items = [
            {
                "quote_id": it.quote_id,
                "supplier_id": it.supplier_id,
                "action": it.action,
                "spec_note": it.spec_note,
                "name_note": it.name_note,
            }
            for it in g.items
        ]
        result.append(AlignmentGroupOut(
            id=g.id,
            project_id=g.project_id,
            category=g.category,
            suggested_name=g.suggested_name,
            suggested_spec=g.suggested_spec,
            suggested_unit=g.suggested_unit,
            suggested_qty=g.suggested_qty,
            confidence=g.confidence,
            reason=g.reason,
            status=g.status,
            items=items,
        ))
    return result


@router.delete("/bid-alignment/groups/{group_id}")
def bid_alignment_delete_group(group_id: int, db: Session = Depends(get_db)):
    """删除一个对齐分组（撤销对齐）。"""
    from apps.api.models.bid_alignment import BidAlignmentGroup
    group = db.query(BidAlignmentGroup).get(group_id)
    if not group:
        raise HTTPException(404, "Alignment group not found")
    db.delete(group)
    db.commit()
    return {"status": "ok", "deleted_group_id": group_id}
