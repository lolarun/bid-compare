"""Analysis and comparison API endpoints — v2."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
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
        # Mode 2: query from DB — sample evenly per supplier to avoid
        # one supplier dominating the LLM context window.
        ROW_CAP = 75
        per_supplier = max(ROW_CAP // len(body.supplier_ids), 20)

        base_q = db.query(Quote, Material, Supplier).join(
            Material, Quote.material_id == Material.id
        ).join(
            Supplier, Quote.supplier_id == Supplier.id
        ).filter(Quote.unit_price > 0)
        if body.project_id:
            base_q = base_q.filter(Quote.project_id == body.project_id)
        if body.category:
            base_q = base_q.filter(Material.category == body.category)

        results = []
        for sid in body.supplier_ids:
            chunk = (
                base_q
                .filter(Quote.supplier_id == sid)
                .order_by(Material.standard_name)
                .limit(per_supplier)
                .all()
            )
            results.extend(chunk)

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
        seen_quotes: set[int] = set()  # dedupe: (group, quote_id) is UNIQUE; LLM may repeat a quote
        for item in g.items:
            if item.quote_id in seen_quotes:
                continue  # same quote listed twice in one group — skip the duplicate
            seen_quotes.add(item.quote_id)
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


@router.get("/anchor-review")
def anchor_review(
    project_id: int = Query(...),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    """人工复核:返回低置信锚点组 + 残差报价,含供应商/物料名称。

    低置信 = group.confidence < 0.70。
    残差   = 本项目/品类的报价中未出现在任何对齐组里的条目。
    """
    import re as _re
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    from apps.api.models.quote import Quote as QuoteModel
    from apps.api.models.material import Material as MaterialModel
    from apps.api.models.supplier import Supplier as SupplierModel

    LOW_CONF = 0.70

    # 所有该 project+category 的已确认锚点组
    groups = (
        db.query(BidAlignmentGroup)
        .filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
        )
        .all()
    )

    # 构建 quote_id → (quote, material, supplier) 映射
    all_rows = (
        db.query(QuoteModel, MaterialModel, SupplierModel)
        .join(MaterialModel, QuoteModel.material_id == MaterialModel.id)
        .outerjoin(SupplierModel, QuoteModel.supplier_id == SupplierModel.id)
        .filter(
            QuoteModel.project_id == project_id,
            MaterialModel.category == category,
        )
        .all()
    )
    quote_map = {qt.id: (qt, mat, sup) for qt, mat, sup in all_rows}

    # 已匹配 quote_id 集合
    matched_ids: set[int] = set()
    for g in groups:
        for item in g.items:
            matched_ids.add(item.quote_id)

    # 残差:未匹配的报价
    residue_quotes = []
    for qt, mat, sup in all_rows:
        if qt.id not in matched_ids:
            residue_quotes.append({
                "quote_id": qt.id,
                "supplier_id": qt.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "unit_price": qt.unit_price,
            })

    # 低置信组(含物料+供应商明细)
    low_conf_groups = []
    for g in groups:
        if g.confidence is None or g.confidence >= LOW_CONF:
            continue
        items = []
        for item in g.items:
            if item.quote_id not in quote_map:
                continue
            qt, mat, sup = quote_map[item.quote_id]
            # 从 spec_note "cos=0.69" 提取余弦值
            cosine = g.confidence  # 默认用组置信度
            if item.spec_note:
                m = _re.search(r"cos=(\d+\.?\d*)", item.spec_note)
                if m:
                    cosine = float(m.group(1))
            items.append({
                "quote_id": item.quote_id,
                "supplier_id": item.supplier_id or qt.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "cosine": cosine,
            })
        # 按余弦降序排列便于快速扫描
        items.sort(key=lambda x: -x["cosine"])
        low_conf_groups.append({
            "group_id": g.id,
            "anchor_name": g.suggested_name,
            "anchor_spec": g.suggested_spec or "",
            "confidence": g.confidence,
            "items": items,
        })

    # 低置信组按置信度升序(最需要关注的在前)
    low_conf_groups.sort(key=lambda x: x["confidence"])

    # 高置信已匹配组
    confirmed_groups = []
    for g in groups:
        if g.confidence is not None and g.confidence < LOW_CONF:
            continue
        items = []
        for item in g.items:
            if item.quote_id not in quote_map:
                continue
            qt, mat, sup = quote_map[item.quote_id]
            cosine = g.confidence or 1.0
            if item.spec_note:
                m = _re.search(r"cos=(\d+\.?\d*)", item.spec_note)
                if m:
                    cosine = float(m.group(1))
            items.append({
                "quote_id": item.quote_id,
                "supplier_id": item.supplier_id or qt.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "cosine": cosine,
            })
        items.sort(key=lambda x: -x["cosine"])
        confirmed_groups.append({
            "group_id": g.id,
            "anchor_name": g.suggested_name,
            "anchor_spec": g.suggested_spec or "",
            "confidence": g.confidence or 1.0,
            "items": items,
        })

    confirmed_groups.sort(key=lambda x: -x["confidence"])

    return {
        "low_conf_groups": low_conf_groups,
        "confirmed_groups": confirmed_groups,
        "residue_quotes": residue_quotes,
    }


@router.post("/tender-list/match")
async def tender_list_match(
    file: UploadFile = File(...),
    project_id: int = Form(...),
    category: str = Form(...),
    supplier_ids: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """锚点模式：上传招标清单 xlsx → 解析锚点 → 嵌入匹配供应商报价 → 落对齐组。

    落组后，现有 /bid-matrix 自动渲染为「锚点行 × 供应商」比价矩阵。
    见 docs/design/05-比价流程的智能化分层.md。
    """
    from apps.api.services.anchor_match import import_and_match

    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "招标清单需为 Excel 文件(.xlsx/.xls)")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file upload")

    sids = None
    if supplier_ids:
        try:
            sids = [int(x) for x in supplier_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "supplier_ids 须为逗号分隔的整数")

    try:
        summary = import_and_match(db, content, project_id, category, sids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        import traceback, logging
        logging.error("tender-list/match error: %s\n%s", e, traceback.format_exc())
        raise HTTPException(500, f"招标清单匹配失败：{type(e).__name__}: {e}")
    return summary.as_dict()
