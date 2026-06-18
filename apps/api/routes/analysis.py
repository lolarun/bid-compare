"""Analysis and comparison API endpoints — v2."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
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
    """横向对比矩阵 — F6.1 核心接口。

    v2.5: When a confirmed TenderListSession exists, uses anchor-full-axis matrix
    (all tender list anchors as rows, cells have cell_status).
    Falls back to original quote-driven matrix when no session found.
    """
    from apps.api.models.alignment_finalization import AlignmentFinalization

    allowed_group_ids = None
    not_finalized_warning = None

    if body.project_id and body.category:
        fin = (
            db.query(AlignmentFinalization)
            .filter(
                AlignmentFinalization.project_id == body.project_id,
                AlignmentFinalization.category == body.category,
                AlignmentFinalization.status == "finalized",
            )
            .order_by(AlignmentFinalization.created_at.desc())
            .first()
        )
        if fin and fin.group_ids_json:
            allowed_group_ids = set(fin.group_ids_json)
        else:
            not_finalized_warning = "对齐审核尚未完成，矩阵使用当前所有已确认组（未锁定快照）"

    # Guard: project_id without category makes no sense in the new anchor flow
    if body.project_id and not body.category:
        raise HTTPException(
            status_code=400,
            detail="project_id 指定时必须同时提供 category（品类）。",
        )

    # v2.5: prefer anchor-full-axis matrix when TenderListSession exists
    result = None
    if body.project_id and body.category:
        from apps.api.models.tender_list_session import TenderListSession
        from apps.api.services.tender_list import rebuild_anchors
        from apps.api.services.bid_matrix import build_anchor_matrix

        session = (
            db.query(TenderListSession)
            .filter(
                TenderListSession.project_id == body.project_id,
                TenderListSession.category == body.category,
                TenderListSession.is_current.is_(True),
            )
            .first()
        )
        if session and session.anchors_json:
            anchors = rebuild_anchors(session)

            result = build_anchor_matrix(
                db,
                anchors=anchors,
                tender_list_session_id=session.id,
                used_submission_ids=session.used_submission_ids or [],
                supplier_ids=body.supplier_ids,
                submission_ids=getattr(body, "submission_ids", []) or [],
                project_id=body.project_id,
                category=body.category,
                allowed_group_ids=allowed_group_ids,
            )
        else:
            # project_id + category present but no confirmed TenderListSession → refuse
            # silently falling back to legacy mode would show 449 rows of all-history quotes
            raise HTTPException(
                status_code=409,
                detail=(
                    f"项目 {body.project_id} / 品类 {body.category} 尚无已确认采购清单"
                    "（TenderListSession）。请先完成采购清单上传和确认步骤。"
                ),
            )

    # Fallback: no project_id or no category — legacy quote-driven mode
    if result is None:
        result = build_bid_matrix(
            db,
            supplier_ids=body.supplier_ids,
            project_id=body.project_id,
            material_ids=body.material_ids,
            category=body.category,
            allowed_group_ids=allowed_group_ids,
        )
        result["anchor_matrix"] = False

    if not_finalized_warning:
        result["not_finalized_warning"] = not_finalized_warning
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

        from apps.api.services.quote_filters import valid_quote_filters as _vqf
        base_q = db.query(Quote, Material, Supplier).join(
            Material, Quote.material_id == Material.id
        ).join(
            Supplier, Quote.supplier_id == Supplier.id
        ).filter(Quote.unit_price > 0, *_vqf())
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
        # dedupe：同一 (group, quote_id) 或 (group, bid_quote_line_id) 不能重复
        seen_quote_ids: set[int] = set()
        seen_bql_ids: set[int] = set()
        for item in g.items:
            if item.bid_quote_line_id is not None:
                if item.bid_quote_line_id in seen_bql_ids:
                    continue
                seen_bql_ids.add(item.bid_quote_line_id)
                ai = BidAlignmentItem(
                    group_id=group.id,
                    bid_quote_line_id=item.bid_quote_line_id,
                    quote_id=None,
                    supplier_id=item.supplier_id,
                    action=item.action,
                    spec_note=item.spec_note,
                    name_note=item.name_note,
                )
            else:
                if item.quote_id in seen_quote_ids:
                    continue
                seen_quote_ids.add(item.quote_id)
                ai = BidAlignmentItem(
                    group_id=group.id,
                    quote_id=item.quote_id,
                    bid_quote_line_id=None,
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
                "bid_quote_line_id": it.bid_quote_line_id,
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


@router.get("/anchor-review/matrix")
def anchor_review_matrix(
    project_id: int = Query(...),
    category: str = Query(...),
    supplier_ids: str | None = Query(None),  # 逗号分隔的供应商 ID
    db: Session = Depends(get_db),
):
    """采购清单维度对齐复核矩阵 — 一行一个采购锚点，N列供应商。

    supplier_ids: 本次比价的供应商集合（逗号分隔整数）。
    若不传，尝试从当前 TenderListSession.confirmed_supplier_ids 恢复。
    两者均为空 → 400，禁止拉历史全量供应商。
    """
    from apps.api.services.bid_matrix import build_anchor_review_matrix
    from apps.api.models.tender_list_session import TenderListSession as _TLS

    sids: list[int] | None = None
    if supplier_ids:
        try:
            sids = [int(x) for x in supplier_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "supplier_ids 须为逗号分隔的整数")

    if not sids:
        # Try recovering from persisted session scope
        _s = db.query(_TLS).filter(
            _TLS.project_id == project_id,
            _TLS.category == category,
            _TLS.is_current == True,  # noqa: E712
            _TLS.status == "confirmed",
        ).first()
        if _s and _s.confirmed_supplier_ids:
            sids = [int(x) for x in _s.confirmed_supplier_ids]

    if not sids:
        raise HTTPException(400, {
            "error": "missing_supplier_ids",
            "message": "必须提供本次比价的供应商 ID（supplier_ids），禁止拉历史全量供应商。"
                       "请先完成供应商报价上传并「开始匹配」后再查看复核矩阵。",
        })

    try:
        result = build_anchor_review_matrix(db, project_id, category, supplier_ids=sids)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return result


@router.get("/anchor-review")
def anchor_review(
    project_id: int = Query(...),
    category: str = Query(...),
    supplier_ids: str | None = Query(None),  # 逗号分隔的供应商 ID
    db: Session = Depends(get_db),
):
    """人工复核:返回低置信锚点组 + 残差报价,含供应商/物料名称。

    低置信 = group.confidence < 0.70。
    残差   = 本项目/品类的报价中未出现在任何对齐组里的条目。
    supplier_ids: 提供时只统计这些供应商的报价，防历史数据污染。
    """
    import re as _re
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    from apps.api.models.quote import Quote as QuoteModel
    from apps.api.models.material import Material as MaterialModel
    from apps.api.models.supplier import Supplier as SupplierModel

    LOW_CONF = 0.70

    sids: list[int] | None = None
    if supplier_ids:
        try:
            sids = [int(x) for x in supplier_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "supplier_ids 须为逗号分隔的整数")

    # v2.7: resolve supplier scope from session if not provided; hard-block if still empty
    from apps.api.models.tender_list_session import TenderListSession as _TLSA
    _cur_session = db.query(_TLSA).filter(
        _TLSA.project_id == project_id,
        _TLSA.category == category,
        _TLSA.is_current == True,  # noqa: E712
        _TLSA.status == "confirmed",
    ).first()
    if not sids and _cur_session and _cur_session.confirmed_supplier_ids:
        sids = [int(x) for x in _cur_session.confirmed_supplier_ids]
    if not sids:
        raise HTTPException(400, {
            "error": "missing_supplier_ids",
            "message": "必须提供本次比价的供应商 ID，请先完成上传匹配后再查看复核数据。",
        })
    _cur_session_id = _cur_session.id if _cur_session else None

    # 只拉当前 TenderListSession 的已确认锚点组（防历史数据污染）
    _grp_q = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
        BidAlignmentGroup.status == "confirmed",
    )
    if _cur_session_id:
        _grp_q = _grp_q.filter(
            BidAlignmentGroup.tender_list_session_id == _cur_session_id
        )
    groups = _grp_q.all()

    # 构建 quote_id → (quote, material, supplier) 映射（旧路径）
    q_base = (
        db.query(QuoteModel, MaterialModel, SupplierModel)
        .join(MaterialModel, QuoteModel.material_id == MaterialModel.id)
        .outerjoin(SupplierModel, QuoteModel.supplier_id == SupplierModel.id)
        .filter(
            QuoteModel.project_id == project_id,
            MaterialModel.category == category,
        )
    )
    if sids:
        q_base = q_base.filter(QuoteModel.supplier_id.in_(sids))
    all_rows = q_base.all()
    quote_map = {qt.id: (qt, mat, sup) for qt, mat, sup in all_rows}

    # 构建 bid_quote_line_id → (bql, supplier) 映射（新路径）
    from apps.api.models.bid_submission import BidQuoteLine as _BQL, BidSubmission as _BidSub
    _all_bql_ids: set[int] = set()
    for g in groups:
        for item in g.items:
            if item.bid_quote_line_id is not None:
                _all_bql_ids.add(item.bid_quote_line_id)
    bql_map: dict = {}
    if _all_bql_ids:
        _bql_rows = (
            db.query(_BQL, SupplierModel)
            .join(_BidSub, _BQL.submission_id == _BidSub.id)
            .join(SupplierModel, _BidSub.supplier_id == SupplierModel.id)
            .filter(_BQL.id.in_(_all_bql_ids))
            .all()
        )
        bql_map = {bql.id: (bql, sup) for bql, sup in _bql_rows}

    # 已匹配集合（两条路径分开追踪）
    matched_quote_ids: set[int] = set()
    matched_bql_ids: set[int] = set()
    for g in groups:
        for item in g.items:
            if item.bid_quote_line_id is not None:
                matched_bql_ids.add(item.bid_quote_line_id)
            elif item.quote_id is not None:
                matched_quote_ids.add(item.quote_id)

    # 残差:未匹配的旧路径报价
    residue_quotes = []
    for qt, mat, sup in all_rows:
        if qt.id not in matched_quote_ids:
            residue_quotes.append({
                "quote_id": qt.id,
                "bid_quote_line_id": None,
                "supplier_id": qt.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "unit_price": qt.unit_price,
            })

    # 残差:未匹配的新路径 BidQuoteLine 行
    _unmatched_bql_q = (
        db.query(_BQL, SupplierModel)
        .join(_BidSub, _BQL.submission_id == _BidSub.id)
        .join(SupplierModel, _BidSub.supplier_id == SupplierModel.id)
        .filter(
            _BidSub.project_id == project_id,
            _BQL.category == category,
        )
    )
    if sids:
        _unmatched_bql_q = _unmatched_bql_q.filter(_BidSub.supplier_id.in_(sids))
    if matched_bql_ids:
        _unmatched_bql_q = _unmatched_bql_q.filter(~_BQL.id.in_(matched_bql_ids))
    for bql, sup in _unmatched_bql_q.all():
        residue_quotes.append({
            "quote_id": None,
            "bid_quote_line_id": bql.id,
            "supplier_id": sup.id if sup else None,
            "supplier_name": sup.name if sup else "",
            "material_name": bql.standard_name,
            "spec": bql.spec or "",
            "unit_price": bql.unit_price,
        })

    def _item_detail(item, quote_map, bql_map, re_mod):
        cosine = None
        if item.spec_note:
            m = re_mod.search(r"cos=(\d+\.?\d*)", item.spec_note)
            if m:
                cosine = float(m.group(1))
        if item.bid_quote_line_id is not None:
            if item.bid_quote_line_id not in bql_map:
                return None
            bql, sup = bql_map[item.bid_quote_line_id]
            return {
                "item_id": item.id,
                "action": item.action,
                "quote_id": None,
                "bid_quote_line_id": item.bid_quote_line_id,
                "supplier_id": item.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": bql.standard_name,
                "spec": bql.spec or "",
                "unit_price": bql.unit_price,
                "cosine": cosine,
                "spec_note": item.spec_note or "",
            }
        else:
            if item.quote_id not in quote_map:
                return None
            qt, mat, sup = quote_map[item.quote_id]
            return {
                "item_id": item.id,
                "action": item.action,
                "quote_id": item.quote_id,
                "bid_quote_line_id": None,
                "supplier_id": item.supplier_id or qt.supplier_id,
                "supplier_name": sup.name if sup else "",
                "material_name": mat.standard_name,
                "spec": mat.spec or "",
                "unit_price": qt.unit_price,
                "cosine": cosine,
                "spec_note": item.spec_note or "",
            }

    # Groups with ANY pending items → need review (item-level)
    # Groups fully confirmed → show in confirmed_groups
    low_conf_groups = []
    confirmed_groups = []

    for g in groups:
        all_items = [_item_detail(it, quote_map, bql_map, _re) for it in g.items]
        all_items = [x for x in all_items if x is not None]
        pending_items = [x for x in all_items if x["action"] == "pending"]
        align_items = [x for x in all_items if x["action"] == "align"]

        if pending_items:
            # Has pending items → surfaces in review queue
            low_conf_groups.append({
                "group_id": g.id,
                "anchor_name": g.suggested_name,
                "anchor_spec": g.suggested_spec or "",
                "confidence": g.confidence,
                "items": sorted(all_items, key=lambda x: (x["action"] != "pending", -(x["cosine"] or 0))),
                "pending_count": len(pending_items),
                "align_count": len(align_items),
            })
        else:
            confirmed_groups.append({
                "group_id": g.id,
                "anchor_name": g.suggested_name,
                "anchor_spec": g.suggested_spec or "",
                "confidence": g.confidence or 1.0,
                "items": sorted(all_items, key=lambda x: -(x["cosine"] or 0)),
            })

    # pending 最多的组排最前(优先处理)
    low_conf_groups.sort(key=lambda x: -x["pending_count"])
    confirmed_groups.sort(key=lambda x: -x["confidence"])

    return {
        "low_conf_groups": low_conf_groups,   # groups needing item-level review
        "confirmed_groups": confirmed_groups,
        "residue_quotes": residue_quotes,
        "pending_items_total": sum(g["pending_count"] for g in low_conf_groups),
    }


class _ItemConfirmBody(BaseModel):
    item_id: int
    action: str  # "align" or "exclude"


@router.post("/anchor-review/item-confirm")
def anchor_review_item_confirm(
    body: _ItemConfirmBody,
    db: Session = Depends(get_db),
):
    """Item 级确认/排除。action=align → 进矩阵；action=exclude → 排除。

    与 group 级 confirm 互补：group confirm 批量处理整组，
    item confirm 精确操作单条低置信报价。
    """
    from apps.api.models.bid_alignment import BidAlignmentItem

    if body.action not in ("align", "exclude"):
        raise HTTPException(400, "action 须为 align 或 exclude")
    item = db.get(BidAlignmentItem, body.item_id)
    if not item:
        raise HTTPException(404, f"BidAlignmentItem {body.item_id} 不存在")
    item.action = body.action
    db.commit()
    return {"ok": True, "item_id": body.item_id, "action": body.action}


@router.post("/tender-list/preview")
async def tender_list_preview(
    file: UploadFile = File(...),
):
    """解析采购清单 xlsx，返回品名/规格/数量预览，不跑嵌入，立即返回。"""
    from apps.api.services.tender_list import parse_tender_xlsx

    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "采购清单需为 Excel 文件(.xlsx/.xls)")
    content = await file.read()
    if not content:
        raise HTTPException(400, "文件为空")
    try:
        anchors = parse_tender_xlsx(content)
    except ValueError as e:
        raise HTTPException(400, str(e))

    from apps.api.services.category_classify import classify_category

    items = []
    breakdown: dict[str, int] = {}
    unknown_count = 0
    for a in anchors:
        g = classify_category(a.name, a.spec, a.pressure, a.material_text())
        if g.is_unknown:
            unknown_count += 1
        else:
            breakdown[g.category] = breakdown.get(g.category, 0) + 1
        items.append({
            "seq": str(a.seq),
            "name": a.name,
            "spec": a.spec,
            "model": a.model,
            "pressure": a.pressure,
            "materials": a.materials,   # dict {col_name: material_text}
            "unit": a.unit,
            "qty": a.qty,
            "profession": a.profession,   # 专业(展示用，不再用于品类识别)
            "category": g.category,       # 品类识别结果("" = 待人工确认)
            "category_confidence": round(g.confidence, 2),
            "category_reason": g.reason,
            "canonical": a.canonical,   # pre-computed valve canonical key
        })

    # detected_category = 多数派品类(基于品名/规格识别，非专业列)
    detected_category = max(breakdown, key=lambda k: breakdown[k]) if breakdown else ""
    return {
        "items": items,
        "detected_category": detected_category,
        "category_breakdown": breakdown,
        "has_multiple_categories": len(breakdown) > 1,
        "unknown_count": unknown_count,
        "total": len(items),
    }


class _ReconcileBody(BaseModel):
    xlsx_items: list  # TenderPreviewItem JSON list
    pdf_items: list   # TenderBidlistResult items JSON list
    source_type: str = "excel_primary"  # "excel_primary" | "pdf_primary"


@router.post("/tender-list/reconcile")
def tender_list_reconcile(body: _ReconcileBody):
    """Excel 清单 vs PDF 投标清单对账。

    source_type="excel_primary"（默认）：Excel 为主，差异须人工确认。
    source_type="pdf_primary"：PDF 为主，Excel 仅参考，差异不阻断流程。
    """
    from apps.api.services.source_reconcile import reconcile_anchors
    return reconcile_anchors(body.xlsx_items, body.pdf_items, source_type=body.source_type)


class _AnchorConfirmBody(BaseModel):
    group_id: int
    action: str  # "confirm" or "reject"


@router.post("/anchor-review/confirm")
def anchor_review_confirm(
    body: _AnchorConfirmBody,
    db: Session = Depends(get_db),
):
    """人工确认/移除 pending 对齐组。
    action=confirm → status=confirmed
    action=reject  → 删除组（级联删 items）
    """
    from apps.api.models.bid_alignment import BidAlignmentGroup

    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, "action 需为 confirm 或 reject")
    group = db.get(BidAlignmentGroup, body.group_id)
    if not group:
        raise HTTPException(404, f"对齐组 {body.group_id} 不存在")
    if body.action == "confirm":
        group.status = "confirmed"
        # Also promote all pending items in this group to align
        for item in group.items:
            if item.action == "pending":
                item.action = "align"
        db.commit()
        return {"ok": True, "group_id": body.group_id, "status": "confirmed"}
    else:
        db.delete(group)
        db.commit()
        return {"ok": True, "group_id": body.group_id, "status": "deleted"}


@router.post("/tender-list/match")
async def tender_list_match(
    file: UploadFile | None = File(None),
    project_id: int = Form(...),
    category: str | None = Form(None),
    supplier_ids: str | None = Form(None),
    submission_ids: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """锚点模式：解析招标清单 → 嵌入匹配供应商报价 → 落对齐组。

    file 可选：提供时直接解析；省略时自动加载当前已确认的 TenderListSession。
    落组后，/bid-matrix 自动渲染为「锚点行 × 供应商」比价矩阵。
    """
    from apps.api.services.anchor_match import import_and_match

    content: bytes | None = None
    if file is not None:
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

    sub_ids = None
    if submission_ids:
        try:
            sub_ids = [int(x) for x in submission_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "submission_ids 须为逗号分隔的整数")

    # If no file provided, reconstruct anchors from current TenderListSession (issue 6)
    prebuilt_anchors = None
    if content is None:
        from apps.api.models.tender_list_session import TenderListSession
        from apps.api.services.tender_list import rebuild_anchors
        tls_q = db.query(TenderListSession).filter(
            TenderListSession.project_id == project_id,
            TenderListSession.is_current.is_(True),
        )
        if category:
            tls_q = tls_q.filter(TenderListSession.category == category)
        session = tls_q.order_by(TenderListSession.id.desc()).first()
        if not session:
            raise HTTPException(
                400,
                "未提供招标清单文件，且未找到已确认的采购清单 (TenderListSession)。"
                "请先上传并确认招标清单，或直接上传 xlsx 文件。"
            )
        if not category:
            category = session.category
        prebuilt_anchors = rebuild_anchors(session)

    # Resolve tender_list_session_id so groups are linked to the session
    _tls_id: int | None = None
    if content is None and session is not None:
        _tls_id = session.id
    elif content is not None:
        # File upload path: find current session, else auto-create from the file
        # (closes the gap where match ran but no session was persisted → matrix 409).
        if not category:
            raise HTTPException(400, "上传招标清单时必须指定 category（品类）")
        from apps.api.models.tender_list_session import TenderListSession as _TLS
        from apps.api.services.tender_list import (
            parse_tender_xlsx, rebuild_anchors, group_anchors_by_category,
        )
        _s = db.query(_TLS).filter(
            _TLS.project_id == project_id,
            _TLS.category == category,
            _TLS.is_current.is_(True),
        ).first()
        if _s:
            _tls_id = _s.id
            # 用该品类 session 的锚点匹配(避免重解析整份多品类文件跨品类误配)
            if _s.anchors_json:
                prebuilt_anchors = rebuild_anchors(_s)
        else:
            # 自动落 session：解析文件 → 按品类分组 → 每个品类建 confirmed session。
            parsed = parse_tender_xlsx(content)
            groups = group_anchors_by_category(parsed, default_category=category)

            # 校验：请求的 category 必须在检测到的品类列表里，防止跨品类污染。
            available = list(groups.keys())
            if available and category not in available:
                raise HTTPException(400, {
                    "error": "category_not_in_file",
                    "message": f"本清单未检测到品类「{category}」，"
                               f"请从以下品类中选择后重试",
                    "available_categories": available,
                })

            file_name = (file.filename or "") if file is not None else ""
            for cat, anchors_json in groups.items():
                s = _save_tender_session(
                    db, project_id, cat, file_name, anchors_json, confirmed_by=None,
                )
                db.flush()
                if cat == category:
                    _tls_id = s.id
            db.commit()
            # 用本品类锚点匹配(避免拿全清单跨品类误配)
            cur = db.query(_TLS).filter(
                _TLS.project_id == project_id,
                _TLS.category == category,
                _TLS.is_current.is_(True),
            ).first()
            if cur:
                prebuilt_anchors = rebuild_anchors(cur)

    # 品牌硬信号上下文（招标文件第13页）：allowed_aliases + 供应商应投品牌
    brand_ctx = None
    if _tls_id:
        from apps.api.models.tender_list_session import TenderListSession as _TLSb
        from apps.api.services.brand_match import build_brand_context
        _tls_b = db.get(_TLSb, _tls_id)
        if _tls_b and (_tls_b.brand_requirement or _tls_b.supplier_brand_map):
            brand_ctx = build_brand_context(
                _tls_b.brand_requirement, _tls_b.supplier_brand_map
            )

    try:
        summary, per_supplier = import_and_match(
            db, content, project_id, category, sids,
            submission_ids=sub_ids,
            anchors=prebuilt_anchors,
            tender_list_session_id=_tls_id,
            brand_ctx=brand_ctx,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        import traceback, logging
        logging.error("tender-list/match error: %s\n%s", e, traceback.format_exc())
        raise HTTPException(500, f"招标清单匹配失败：{type(e).__name__}: {e}")

    # Enrich per_supplier (now keyed by submission_id) with names and doc_meta
    from apps.api.models import ExtractionJob, Supplier as SupplierModel
    from apps.api.models.bid_submission import BidSubmission as _BSSub
    from apps.api.services.quote_readiness import assess_readiness

    # Load BidSubmission records for submission-keyed stats
    sub_ids_for_lookup = [sid for sid in per_supplier.keys() if isinstance(sid, int) and sid > 0]
    sub_records: dict[int, _BSSub] = {}
    for sub in db.query(_BSSub).filter(_BSSub.id.in_(sub_ids_for_lookup)).all():
        sub_records[sub.id] = sub

    # Build display name: prefer Supplier.name if supplier_id set, else supplier_raw_name
    sub_display_names: dict[int, str] = {}
    for sub_id, sub in sub_records.items():
        if sub.supplier_id:
            sup = db.get(SupplierModel, sub.supplier_id)
            sub_display_names[sub_id] = sup.name if sup else sub.supplier_raw_name
        else:
            sub_display_names[sub_id] = sub.supplier_raw_name

    # Also include legacy supplier_id-keyed entries (old data)
    legacy_sids = [sid for sid in per_supplier.keys() if sid not in sub_records]
    for sup in db.query(SupplierModel).filter(SupplierModel.id.in_(legacy_sids)).all():
        sub_display_names[sup.id] = sup.name

    # Load doc_meta from each submission's extraction job
    doc_meta_by_sub: dict[int, dict] = {}
    for sub_id, sub in sub_records.items():
        job = db.get(ExtractionJob, sub.job_id)
        if job and job.result:
            dm = job.result.get("_doc_meta")
            if dm:
                doc_meta_by_sub[sub_id] = dm

    readiness_list = []
    for stat_key, stats in per_supplier.items():
        sup_name = sub_display_names.get(stat_key, f"supplier_{stat_key}")
        dm = doc_meta_by_sub.get(stat_key)
        r = assess_readiness(stat_key, sup_name, stats, doc_meta=dm)
        readiness_list.append(r.as_dict())

    # v2.7+: persist used_submission_ids on TenderListSession
    if _tls_id:
        from apps.api.models.tender_list_session import TenderListSession as _TLSup
        _tls_obj = db.get(_TLSup, _tls_id)
        if _tls_obj:
            if sids:
                _tls_obj.confirmed_supplier_ids = sorted(set(sids))
            # per_supplier is now keyed by submission_id — persist those IDs directly
            _tls_obj.used_submission_ids = sorted(
                k for k in per_supplier.keys() if k in sub_records
            )
            db.commit()

    result = summary.as_dict()
    result["readiness_list"] = readiness_list
    result["per_supplier_stats"] = per_supplier
    result["category"] = category  # 回传品类（category 为 None 时已从 session 推导）
    return result


# ═══════════════════════════════════════════════════════════════════
#  LLM 供应商视角填采购清单(replace 模式)
# ═══════════════════════════════════════════════════════════════════

class _LlmFillBody(BaseModel):
    project_id: int
    category: str
    supplier_ids: list[int] = []
    tender_list_session_id: int | None = None
    k: int = 3
    mode: str = "replace"
    model: str | None = None
    force_partial: bool = False  # 允许部分供应商失败时仍落库，默认拒绝


def _load_supplier_fill_rows(db, project_id: int, category: str, supplier_id: int):
    """路由主线程读 DB → 纯数据 SupplierQuoteRow(worker 不碰 DB)。

    BQL 优先：如果该供应商有 active BidSubmission，从 BidQuoteLine 读行数据，
    不从 Quote 读（因为 archive 可能尚未执行）。无 BidSubmission 则退回 Quote 路径。
    """
    from apps.api.models import Quote, Material
    from apps.api.services.supplier_fill_llm import SupplierQuoteRow
    from apps.api.services.canonical import extract_valve_canonical
    from apps.api.services.bid_submission_resolve import resolve_active_submissions

    # ── BQL 路径（优先）──────────────────────────────────────────────────────
    active_subs = resolve_active_submissions(db, project_id, category, [supplier_id])
    matching_sub = next((s for s in active_subs.values() if s.supplier_id == supplier_id), None)
    if matching_sub:
        from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
        sub = matching_sub
        bql_rows = (
            db.query(BidQuoteLine)
            .filter(
                BidQuoteLine.submission_id == sub.id,
                BidQuoteLine.category == category,
            )
            .all()
        )
        out = []
        for bql in bql_rows:
            canon = bql.canonical or extract_valve_canonical(bql.standard_name or "", bql.spec or "")
            out.append(SupplierQuoteRow(
                quote_id=bql.id,           # used as row identifier by LLM worker
                bid_quote_line_id=bql.id,  # signals BQL path to _persist_llm_fill
                supplier_id=supplier_id,
                raw_material=bql.standard_name or "",
                raw_spec=bql.spec or "",
                raw_unit=bql.unit or "",
                material=bql.standard_name or "",
                spec=bql.spec or "",
                unit=bql.unit or "",
                qty=bql.qty,
                unit_price=bql.unit_price,
                total_price=bql.total_price,
                canonical=canon or {},
            ))
        return out

    # ── Quote 路径（旧数据 / 已归档）──────────────────────────────────────────
    from apps.api.models.supplier import Supplier as _Sup
    from apps.api.services.quote_filters import valid_quote_filters as _vqf2
    q = (
        db.query(Quote, Material)
        .join(Material, Quote.material_id == Material.id)
        .join(_Sup, Quote.supplier_id == _Sup.id)
        .filter(Quote.project_id == project_id, Quote.supplier_id == supplier_id, *_vqf2())
    )
    if category:
        q = q.filter(Material.category == category)
    out = []
    for qt, m in q.all():
        ext = m.extended_attrs or {}
        meta = qt.extraction_meta_json or {}
        meta_canon = meta.get("canonical") or {}
        mat_canon = ext.get("canonical") or extract_valve_canonical(
            m.standard_name or "", m.spec or "", material=m.material_type or ""
        )
        canon = meta_canon if meta_canon.get("valve_type") or meta_canon.get("dn") else mat_canon
        norm_mat = str(meta.get("normalized_material") or ext.get("normalized_material") or "").strip()
        ocr_reason = str(meta.get("ocr_correction_reason") or ext.get("ocr_correction_reason") or "").strip()
        out.append(SupplierQuoteRow(
            quote_id=qt.id,
            supplier_id=qt.supplier_id or 0,
            raw_material=meta.get("raw_material") or m.standard_name or "",
            raw_spec=meta.get("raw_spec") or m.spec or "",
            raw_unit=meta.get("raw_unit") or m.unit or "",
            raw_remark=meta.get("raw_remark") or "",
            material=m.standard_name or "",
            spec=m.spec or "",
            unit=m.unit or "",
            qty=qt.quantity,
            unit_price=qt.unit_price,
            total_price=qt.total_price,
            material_type=str(m.material_type or "").strip(),
            normalized_material=norm_mat,
            ocr_correction_reason=ocr_reason,
            canonical=canon or {},
        ))
    return out


def _persist_llm_fill(
    db, project_id, category, session_id, results, seq_to_anchor, valid_sids,
    bql_supplier_ids: set | None = None,
    bql_supplier_to_submission: dict | None = None,
):
    """单写者一次性落库：软删旧组（superseded）→ 按 anchor_seq 建组 → 每 cell 一 item。

    软删而非物理删，保留历史可追溯；bid_matrix 只读 status='confirmed' 故不受影响。
    bql_supplier_ids: supplier_ids whose rows came from BidQuoteLine (cell.quote_id is
    actually a BidQuoteLine.id — must be written to bid_quote_line_id, not quote_id).
    bql_supplier_to_submission: supplier_id → submission_id mapping for BQL path.
    """
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem

    bql_sids: set = bql_supplier_ids or set()

    # replace 语义：将该 project/category 下所有旧 confirmed 组标为 superseded（软删）
    old_confirmed = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
        BidAlignmentGroup.status == "confirmed",
    ).all()
    for g in old_confirmed:
        g.status = "superseded"
    db.flush()

    cells_by_seq: dict[int, list] = {}
    for res in results:
        for cell in res.cells:
            cells_by_seq.setdefault(cell.anchor_seq, []).append(cell)

    for seq, cells in cells_by_seq.items():
        anchor = seq_to_anchor.get(seq)
        name = anchor.name if anchor else f"#{seq}"
        spec = ""
        unit = ""
        qty = None
        if anchor:
            spec = " ".join(
                x for x in [anchor.spec, anchor.pressure, anchor.material_text()] if x
            ).strip()
            unit = anchor.unit
            qty = anchor.qty
        conf = min((c.confidence for c in cells), default=0.0)
        group = BidAlignmentGroup(
            project_id=project_id, category=category,
            suggested_name=name, suggested_spec=spec, suggested_unit=unit, suggested_qty=qty,
            confidence=round(conf, 3), reason=f"[llm-fill] #{seq}",
            status="confirmed", tender_list_session_id=session_id, anchor_seq=str(seq),
        )
        db.add(group)
        db.flush()
        for cell in cells:
            qsid = cell.supplier_id if cell.supplier_id in valid_sids else None
            is_bql = cell.supplier_id in bql_sids
            row_id = cell.quote_id if cell.quote_id else None
            # pending with no row ref can't satisfy NOT NULL FK — skip DB row,
            # but the cell is still returned in the API response.
            if row_id is None:
                continue
            note = f"LLM cos={cell.confidence:.2f}"
            _submission_id = (
                (bql_supplier_to_submission or {}).get(cell.supplier_id) if is_bql else None
            )
            if is_bql:
                db.add(BidAlignmentItem(
                    group_id=group.id,
                    bid_quote_line_id=row_id,
                    quote_id=None,
                    supplier_id=qsid,
                    submission_id=_submission_id,
                    action=cell.action, spec_note=note.strip()[:500],
                    name_note=(cell.reason or "")[:500],
                    agg_total=cell.agg_total, agg_qty=cell.agg_qty,
                ))
            else:
                db.add(BidAlignmentItem(
                    group_id=group.id,
                    quote_id=row_id,
                    bid_quote_line_id=None,
                    supplier_id=qsid,
                    submission_id=None,
                    action=cell.action, spec_note=note.strip()[:500],
                    name_note=(cell.reason or "")[:500],
                    agg_total=cell.agg_total, agg_qty=cell.agg_qty,
                ))


# ─── Dynamic suspect anchor selection ────────────────────────────────────────

_RISKY_FLAGS: frozenset[str] = frozenset({
    "canonical_conflict",
    "valve_type_conflict",
    "risky_candidate",
    "dup_qids",
    "missing_without_evidence",
})
_RISKY_FLAG_PREFIXES: tuple[str, ...] = ("ac_conflict", "ocr_corrected")


def _select_suspect_anchor_seqs(anchors, results, supplier_ids: list[int]) -> set[int]:
    """Dynamically select anchor seqs needing AC re-evaluation or audit.

    An anchor is suspect (any criterion):
    - quoted_count < 2  (fewer than 2 suppliers aligned)
    - covered_count < N (at least one supplier has no align/pending cell)
    - any pending cell  (LLM uncertain, needs AC confirmation)
    - any cell carries a risky / conflict flag
    - a supplier with residue_high_cos rows did NOT align this anchor
    """
    N = len(supplier_ids)
    cells_by_anchor: dict[int, list] = {}
    residue_high_cos_sids: set[int] = set()
    for res in results:
        if res.residue_high_cos:
            residue_high_cos_sids.add(res.supplier_id)
        for cell in res.cells:
            cells_by_anchor.setdefault(cell.anchor_seq, []).append(cell)

    suspect: set[int] = set()
    for anchor in anchors:
        seq = int(anchor.seq)
        cells = cells_by_anchor.get(seq, [])
        q = sum(1 for c in cells if c.action == "align")
        cov = sum(1 for c in cells if c.action in ("align", "pending"))
        if q < 2 or cov < N or any(c.action == "pending" for c in cells):
            suspect.add(seq)
            continue
        for c in cells:
            for flag in (c.flags or []):
                if flag in _RISKY_FLAGS or any(flag.startswith(p) for p in _RISKY_FLAG_PREFIXES):
                    suspect.add(seq)
                    break
            if seq in suspect:
                break
        if seq not in suspect and residue_high_cos_sids:
            for sid in residue_high_cos_sids:
                sid_cell = next((c for c in cells if c.supplier_id == sid), None)
                if sid_cell is None or sid_cell.action != "align":
                    suspect.add(seq)
                    break

    return suspect


@router.post("/tender-list/llm-fill")
async def tender_list_llm_fill(body: _LlmFillBody, db: Session = Depends(get_db)):
    """N 个供应商填表 LLM 代理(replace 模式)。

    路由主线程读 anchors + supplier rows → worker(纯数据) attach_topk+LLM+validate →
    主线程单写者落库 [llm-fill] 组。replace 会失效旧 AlignmentFinalization。
    """
    import asyncio
    import os
    from apps.api.models.tender_list_session import TenderListSession
    from apps.api.models import Supplier, Quote, Material
    from apps.api.models.alignment_finalization import AlignmentFinalization
    from apps.api.services.tender_list import rebuild_anchors
    from apps.api.services.anchor_match import embed_anchor_vecs, _embed_client
    from apps.api.services.supplier_fill_llm import (
        AnchorView, fill_one_supplier, fill_one_supplier_anchor_centric, DEFAULT_FILL_MODEL,
    )

    if body.mode != "replace":
        raise HTTPException(400, "v1 仅支持 mode='replace'")

    # 1. 解析 session + 重建锚点
    sq = db.query(TenderListSession).filter(
        TenderListSession.project_id == body.project_id,
        TenderListSession.category == body.category,
    )
    if body.tender_list_session_id is not None:
        session = db.get(TenderListSession, body.tender_list_session_id)
    else:
        session = sq.filter(TenderListSession.is_current.is_(True)).first()
    if not session or not session.anchors_json:
        raise HTTPException(400, "未找到已确认的采购清单 (TenderListSession)")

    tender_anchors = rebuild_anchors(session)
    anchor_views = [
        AnchorView(seq=int(a.seq), name=a.name, spec=a.spec, pressure=a.pressure,
                   unit=a.unit, qty=a.qty, canonical=a.canonical or {})
        for a in tender_anchors
    ]
    seq_to_anchor = {int(a.seq): a for a in tender_anchors}

    # 2. 解析供应商集合
    sids = list(body.supplier_ids)
    if not sids:
        from apps.api.services.quote_filters import valid_quote_filters as _vqf3
        q = (
            db.query(Quote.supplier_id)
            .join(Material, Quote.material_id == Material.id)
            .join(Supplier, Quote.supplier_id == Supplier.id)
            .filter(Quote.project_id == body.project_id, Material.category == body.category, *_vqf3())
            .distinct()
        )
        sids = [r[0] for r in q.all() if r[0]]
    if not sids:
        raise HTTPException(400, "该项目/品类下没有可填表的供应商报价")

    sup_names = {
        s.id: s.name for s in db.query(Supplier).filter(Supplier.id.in_(sids)).all()
    }
    valid_sids = {row[0] for row in db.query(Supplier.id).all()}

    # 3. 主线程读：每家 supplier rows(纯数据)；顺带记录哪些走 BQL 路径
    from apps.api.services.bid_submission_resolve import resolve_active_submissions as _ras_fill
    _fill_active_subs = _ras_fill(db, body.project_id, body.category, sids)
    # _fill_active_subs is keyed by submission_id; derive supplier-based lookups for LLM fill path
    _bql_actual_supplier_ids: set[int] = {
        sub.supplier_id for sub in _fill_active_subs.values() if sub.supplier_id
    }
    _bql_supplier_to_submission: dict[int, int] = {
        sub.supplier_id: sub_id
        for sub_id, sub in _fill_active_subs.items()
        if sub.supplier_id
    }
    rows_by_sid = {sid: _load_supplier_fill_rows(db, body.project_id, body.category, sid)
                   for sid in sids}

    # 4. 锚点向量只算一次(在 executor，避免阻塞事件循环)
    client = _embed_client()
    loop = asyncio.get_event_loop()
    anchor_vecs = await loop.run_in_executor(None, embed_anchor_vecs, tender_anchors, client)

    thinking_model = os.environ.get("SUPPLIER_FILL_THINKING_MODEL") or None

    # 5. 并发 worker(纯数据，不碰 DB)
    sem = asyncio.Semaphore(3)

    async def _run(sid):
        async with sem:
            return sid, await loop.run_in_executor(
                None,
                lambda: fill_one_supplier(
                    rows_by_sid[sid], anchor_views, client,
                    supplier_name=sup_names.get(sid, str(sid)),
                    anchor_vecs=anchor_vecs, model=body.model,
                    thinking_model=thinking_model, k=body.k,
                ),
            )

    gathered = await asyncio.gather(*[_run(sid) for sid in sids])
    results_by_sid = dict(gathered)
    results = list(results_by_sid.values())

    # 5b. Anchor-centric gap pass (Wave 2): for every anchor with <2 aligned suppliers,
    # re-run anchor-centric fill with ALL rows (not just residue) so OCR-corrupted
    # rows that were missed in the first pass can be recovered.
    # Suspect anchors are always included in the gap pass even if already ≥2 aligned —
    # they need independent verification to catch first-pass mismatches.
    _align_sids_1: dict[int, set] = {}
    for _res in results:
        for _cell in _res.cells:
            if _cell.action == "align":
                _align_sids_1.setdefault(_cell.anchor_seq, set()).add(_cell.supplier_id)
    # Select suspect anchors dynamically from Wave-1 results (risky flags, pending, coverage gaps)
    _suspect_seqs_1 = _select_suspect_anchor_seqs(tender_anchors, results, sids)
    # Include suspect seqs even when ≥2 aligned (need AC confirmation)
    gap_seqs = [
        int(a.seq) for a in tender_anchors
        if len(_align_sids_1.get(int(a.seq), set())) < 2 or int(a.seq) in _suspect_seqs_1
    ]

    if gap_seqs:
        _gap_seq_set = set(gap_seqs)
        gap_anchor_views = [av for av in anchor_views if int(av.seq) in _gap_seq_set]

        async def _run_ac(sid):
            async with sem:
                # already_aligned_seqs: skip non-suspect anchors this supplier already confirmed.
                # Suspect anchors are NOT skipped — AC must re-verify them independently.
                _aligned_non_suspect = {
                    _c.anchor_seq for _c in results_by_sid[sid].cells
                    if _c.action == "align" and _c.anchor_seq not in _suspect_seqs_1
                }
                try:
                    _res = await loop.run_in_executor(
                        None,
                        lambda _sid=sid, _al=_aligned_non_suspect: fill_one_supplier_anchor_centric(
                            rows_by_sid[_sid],        # ALL rows, not just residue
                            gap_anchor_views,
                            client,
                            supplier_name=sup_names.get(_sid, str(_sid)),
                            anchor_vecs=None,         # gap subset ≠ full anchor_vecs; re-embed
                            model=body.model,
                            already_aligned_seqs=_al,
                        ),
                    )
                except Exception as _exc:
                    from apps.api.services.supplier_fill_llm import SupplierFillResult
                    _res = SupplierFillResult(supplier_id=sid, error=str(_exc))
                return sid, _res

        ac_gathered = await asyncio.gather(*[_run_ac(sid) for sid in sids])
        for _sid, _ac in ac_gathered:
            if _ac.error and not results_by_sid[_sid].error:
                results_by_sid[_sid].error = f"anchor_centric: {_ac.error}"
            _main = results_by_sid[_sid]
            _main.tokens_used += _ac.tokens_used
            _main.dropped.extend(_ac.dropped)   # preserve llm_missing evidence for missing_audit

            for _c in _ac.cells:
                _seq = _c.anchor_seq
                _existing = next((c for c in _main.cells if c.anchor_seq == _seq), None)

                if _existing is None:
                    # New anchor coverage from AC pass — just add
                    _main.cells.append(_c)
                    continue

                if _seq in _suspect_seqs_1:
                    # Suspect anchor: smart merge
                    if _existing.action == "pending" and _c.action == "align":
                        # AC upgraded pending → align: replace
                        _main.cells = [c for c in _main.cells if c.anchor_seq != _seq]
                        _main.cells.append(_c)
                    elif _existing.action == "align" and _c.action == "align" and _existing.quote_id != _c.quote_id:
                        # Conflict: two different quotes claimed — downgrade to pending
                        _existing.action = "pending"
                        _existing.status = "pending"
                        _existing.flags = list(_existing.flags or []) + [f"ac_conflict:qid={_c.quote_id}"]
                        _existing.reason = f"{_existing.reason} | AC says qid={_c.quote_id}: {_c.reason}"
                    # else: existing align ≥ AC result → keep existing
                # else: non-suspect, existing cell already there from first pass — skip

            # Cells added by AC pass resolve consumed quote_ids in residue
            _consumed_qids = {_c.quote_id for _c in _ac.cells if _c.action == "align" and _c.quote_id}
            _main.residue_quote_ids = [q for q in _main.residue_quote_ids if q not in _consumed_qids]

    # 6a. 安全闸门：任一供应商 LLM 失败 → 拒绝落库，除非 force_partial=True
    if not body.force_partial:
        failed = [(sid, results_by_sid[sid].error) for sid in sids if results_by_sid[sid].error]
        if failed:
            detail = "; ".join(f"sid={sid}: {err[:120]}" for sid, err in failed)
            raise HTTPException(
                422,
                f"{len(failed)} supplier(s) failed LLM fill — old data preserved. "
                f"Pass force_partial=true to persist partial results. Errors: {detail}",
            )

    # 6b. 单写者落库 + 失效旧 finalization(replace 闭环)
    _persist_llm_fill(db, body.project_id, body.category, session.id,
                      results, seq_to_anchor, valid_sids,
                      bql_supplier_ids=_bql_actual_supplier_ids,
                      bql_supplier_to_submission=_bql_supplier_to_submission)
    n_fin = db.query(AlignmentFinalization).filter(
        AlignmentFinalization.project_id == body.project_id,
        AlignmentFinalization.category == body.category,
        AlignmentFinalization.status == "finalized",
    ).update({"status": "superseded"})
    db.commit()

    # 8. matrix_distribution：基于落库后矩阵，与 /bid-matrix 同源
    from apps.api.services.bid_matrix import build_anchor_matrix as _bam_fn
    from apps.api.services.matrix_stats import build_matrix_distribution_from_rows as _mdr_fn
    _bam_result = _bam_fn(
        db, tender_anchors, session.id, sids, body.project_id, body.category,
        used_submission_ids=[], submission_ids=[],
    )
    matrix_distribution = _mdr_fn(_bam_result["rows"], sids)

    # 7. 指标：新验收标准 quoted/pending≥2 + quoted≥2 + embedding 基线
    llm_align_sids: dict[int, set] = {}   # quoted/aggregated only
    llm_any_sids: dict[int, set] = {}     # quoted/aggregated OR pending
    for res in results:
        for cell in res.cells:
            if cell.action == "align":
                llm_align_sids.setdefault(cell.anchor_seq, set()).add(cell.supplier_id)
                llm_any_sids.setdefault(cell.anchor_seq, set()).add(cell.supplier_id)
            elif cell.action == "pending":
                llm_any_sids.setdefault(cell.anchor_seq, set()).add(cell.supplier_id)
    emb_anchor_sids: dict[int, set] = {}
    for sid in sids:
        for r in rows_by_sid[sid]:
            if r.topk:
                emb_anchor_sids.setdefault(r.topk[0][0], set()).add(sid)

    # In-memory values used only for missing_audit (audit tool, not primary metrics)
    anchors_covered = len(llm_align_sids)
    comparable_2plus_emb = sum(1 for s in emb_anchor_sids.values() if len(s) >= 2)

    # Authoritative metrics from DB-backed matrix_distribution (same source as /bid-matrix)
    comparable_2plus_quoted = matrix_distribution["quoted_ge_2_count"]   # 可比价锚点（quoted ≥2家）
    comparable_2plus = matrix_distribution["covered_ge_2_count"]         # covered ≥2家（含 pending）
    three_way = matrix_distribution["quoted_full_count"]                  # N/N quoted，仅作兼容

    per_supplier_fill = []
    dropped_audit = []
    for sid in sids:
        res = results_by_sid[sid]
        c = res.counts()
        per_supplier_fill.append({
            "supplier_id": sid,
            "supplier_name": sup_names.get(sid, str(sid)),
            "quoted": c["quoted"], "aggregated": c["aggregated"],
            "pending": c["pending"], "excluded": c["excluded"],
            "residue": len(res.residue_quote_ids),
            "residue_high_cos": res.residue_high_cos,
            "dropped": len(res.dropped),
            "tokens_used": res.tokens_used, "duration_ms": res.duration_ms,
            "error": res.error or None,
        })
        for d in res.dropped:
            dropped_audit.append({"supplier_id": sid, **d})

    # missing_audit: per-anchor evidence for anchors still <2 quoted or flagged suspect
    # _suspect_seqs_audit re-evaluated from merged (Wave-1 + AC) results
    _suspect_seqs_audit = _select_suspect_anchor_seqs(tender_anchors, results, sids)

    missing_audit: list[dict] = []
    for a in tender_anchors:
        seq = int(a.seq)
        quoted_n = len(llm_align_sids.get(seq, set()))
        if quoted_n >= 2 and seq not in _suspect_seqs_audit:
            continue
        supplier_detail = []
        for sid in sids:
            res = results_by_sid[sid]
            sid_cell = next((c for c in res.cells if c.anchor_seq == seq), None)
            # Gather nearest candidates from dropped (llm_missing evidence) or residue
            nearest = []
            for d in res.dropped:
                if d.get("anchor_seq") == seq and d.get("reason") == "llm_missing":
                    nearest = d.get("nearest_quote_candidates") or []
                    break
            supplier_detail.append({
                "supplier_id": sid,
                "supplier_name": sup_names.get(sid, str(sid)),
                "status": sid_cell.status if sid_cell else "missing",
                "quote_id": sid_cell.quote_id if sid_cell else None,
                "confidence": sid_cell.confidence if sid_cell else 0.0,
                "flags": sid_cell.flags if sid_cell else [],
                "nearest_quote_candidates": nearest[:3],
            })
        missing_audit.append({
            "anchor_seq": seq,
            "anchor_name": a.name,
            "anchor_spec": a.spec,
            "quoted_count": quoted_n,
            "is_suspect": seq in _suspect_seqs_audit,
            "suppliers": supplier_detail,
        })
    missing_audit.sort(key=lambda x: (not x["is_suspect"], x["quoted_count"], x["anchor_seq"]))
    missing_audit_total = len(missing_audit)
    missing_audit_truncated = missing_audit_total > 60
    missing_audit = missing_audit[:60]

    # false_positive_audit: cells downgraded by the valve_type_conflict gate.
    # A non-empty list means the gate caught real mismatches (good).
    # false_positive_align_count: quoted/aggregated cells that still carry a
    # valve_type_conflict flag — should be 0 if the gate fired correctly.
    false_positive_audit = sorted([
        {
            "supplier_id": res.supplier_id,
            "supplier_name": sup_names.get(res.supplier_id, str(res.supplier_id)),
            **d,
        }
        for res in results
        for d in res.dropped
        if d.get("reason") == "valve_type_conflict"
    ], key=lambda x: (x.get("anchor_seq", 0), x.get("supplier_id", 0)))

    false_positive_align_count = sum(
        1 for res in results
        for cell in res.cells
        if cell.status in ("quoted", "aggregated")
        and any("valve_type_conflict" in f for f in (cell.flags or []))
    )

    missing_without_evidence_count = sum(
        1 for res in results
        for cell in res.cells
        if cell.status == "pending"
        and "missing_without_evidence" in (cell.flags or [])
    )
    supplier_error_count = sum(1 for f in per_supplier_fill if f.get("error"))
    _readiness_warnings: list[str] = []
    if false_positive_align_count > 0:
        _readiness_warnings.append(f"false_positive_align_count={false_positive_align_count}：quoted/agg 中存在阀型冲突，请先处理")
    if missing_without_evidence_count > 0:
        _readiness_warnings.append(f"missing_without_evidence={missing_without_evidence_count}：pending 中有无证据的 missing，需人工复核")
    if supplier_error_count > 0:
        _readiness_warnings.append(f"supplier_error_count={supplier_error_count}：部分供应商 LLM 调用失败")
    readiness = {
        "can_finalize": (
            false_positive_align_count == 0
            and missing_without_evidence_count == 0
            and supplier_error_count == 0
        ),
        "false_positive_align_count": false_positive_align_count,
        "missing_without_evidence_count": missing_without_evidence_count,
        "supplier_error_count": supplier_error_count,
        "warnings": _readiness_warnings,
    }

    return {
        "anchors_total": len(tender_anchors),
        "comparable_2plus": comparable_2plus,          # ≥2 quoted+pending (新主指标)
        "comparable_2plus_quoted": comparable_2plus_quoted,  # ≥2 quoted 仅(旧主指标)
        # three_way kept for backward compat; use matrix_distribution.quoted_full_count for N/N coverage
        "three_way": three_way,
        "anchors_covered": anchors_covered,
        "comparable_2plus_embedding_baseline": comparable_2plus_emb,
        "per_supplier_fill": per_supplier_fill,
        "finalization_invalidated": bool(n_fin),
        "dropped_audit": dropped_audit[:50],
        "missing_audit": missing_audit,
        "missing_audit_total": missing_audit_total,
        "missing_audit_truncated": missing_audit_truncated,
        "false_positive_audit": false_positive_audit,
        "false_positive_align_count": false_positive_align_count,
        "readiness": readiness,
        "matrix_distribution": matrix_distribution,
    }


# ═══════════════════════════════════════════════════════════════════
#  v2.4 审核闸门端点
# ═══════════════════════════════════════════════════════════════════

# ── 采购清单闸门 ──────────────────────────────────────────────────

class _TenderListConfirmBody(BaseModel):
    project_id: int | None = None
    category: str
    file_name: str = ""
    anchors_json: list = []
    anchors_total: int = 0
    confirmed_by: str = ""
    force: bool = False  # 显式强制：unknown 项归入默认品类并写入审计标记
    source_type: str = "excel"  # excel | pdf — 基础清单来源
    brand_requirement: list | None = None   # PDF 第13页业主品牌要求
    supplier_brands: list | None = None      # PDF 第13页投标单位参与品牌 [{supplier_name, brand}]


def _resolve_supplier_brands(db, supplier_brands: list | None) -> list | None:
    """把第13页 [{supplier_name, brand}] 解析到 supplier_id（按公司名模糊匹配）。

    匹配不上保留 supplier_id=None（不静默丢弃），匹配上则附加 supplier_id。
    """
    if not supplier_brands:
        return supplier_brands
    from apps.api.models import Supplier

    suppliers = db.query(Supplier).all()
    resolved: list[dict] = []
    for sb in supplier_brands:
        if not isinstance(sb, dict):
            continue
        name = str(sb.get("supplier_name") or "").strip()
        brand = str(sb.get("brand") or "").strip()
        sid = None
        if name:
            # 双向包含匹配：处理「上海绵存机电设备有限公司」vs「上海绵存」简称
            for sup in suppliers:
                sn = (sup.name or "").strip()
                if not sn:
                    continue
                if sn == name or sn in name or name in sn or (sup.short_name and sup.short_name in name):
                    sid = sup.id
                    break
        if sid is None and name:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "supplier_brand_map: 未能匹配供应商 '%s'(品牌 %s) 到现有供应商", name, brand
            )
        resolved.append({"supplier_name": name, "brand": brand, "supplier_id": sid})
    return resolved


def _save_tender_session(
    db, project_id, category, file_name, anchors_json, confirmed_by,
    source_type="excel", brand_requirement=None, supplier_brand_map=None,
):
    """新建一个 confirmed TenderListSession，旧的同 (project,category) 版本设为非当前。

    返回新建的 session(未 commit；由调用方统一 commit)。
    """
    from apps.api.models.tender_list_session import TenderListSession
    from datetime import datetime as _dt

    db.query(TenderListSession).filter(
        TenderListSession.project_id == project_id,
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    ).update({"is_current": False, "superseded_at": _dt.utcnow()})

    last = (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
        )
        .order_by(TenderListSession.version.desc())
        .first()
    )
    new_version = (last.version + 1) if last else 1

    session = TenderListSession(
        project_id=project_id,
        category=category,
        file_name=file_name,
        source_type=source_type,
        anchors_total=len(anchors_json),
        anchors_json=anchors_json,
        brand_requirement=brand_requirement,
        supplier_brand_map=supplier_brand_map,
        version=new_version,
        is_current=True,
        status="confirmed",
        confirmed_by=confirmed_by or None,
        confirmed_at=_dt.utcnow(),
    )
    db.add(session)
    return session


@router.post("/tender-list/confirm")
def tender_list_confirm(
    body: _TenderListConfirmBody,
    db: Session = Depends(get_db),
):
    """保存 TenderListSession。按 anchor.category 拆分：单品类1个、多品类N个。

    每个 anchor 的品类取自 anchors_json[].category(preview 识别结果)；
    缺失/空(unknown)项：force=False 时 400 拦截，force=True 时归入 body.category 并标记 _category_forced=True。
    """
    # unknown 品类强制拦截
    unknown_items = [
        (a.get("name", "") if isinstance(a, dict) else "")
        for a in (body.anchors_json or [])
        if isinstance(a, dict) and not a.get("category")
    ]
    if unknown_items and not body.force:
        raise HTTPException(400, {
            "error": "unknown_categories",
            "message": f"有 {len(unknown_items)} 项采购品类未识别，请核对后重试，"
                       "或勾选「强制归入默认品类」后再确认",
            "unknown_count": len(unknown_items),
            "unknown_items": unknown_items[:10],
        })

    # 按品类分组 anchors
    groups: dict[str, list] = {}
    for a in (body.anchors_json or []):
        if isinstance(a, dict):
            cat = a.get("category") or ""
            if not cat:
                cat = body.category
                a = {**a, "_category_forced": True}  # 审计标记
        else:
            cat = body.category
        if not cat:
            raise HTTPException(
                400, "存在未识别品类的采购项，且未提供默认品类(category)，无法保存。"
            )
        groups.setdefault(cat, []).append(a)

    if not groups:
        # 空清单：保留旧行为，按 body.category 建空 session
        if not body.category:
            raise HTTPException(400, "category 不能为空")
        groups[body.category] = []

    # PDF 来源：解析第13页供应商-品牌映射到 supplier_id（所有品类共享）
    supplier_brand_map = _resolve_supplier_brands(db, body.supplier_brands)

    sessions_out = []
    for cat, anchors in groups.items():
        s = _save_tender_session(
            db, body.project_id, cat, body.file_name, anchors, body.confirmed_by,
            source_type=body.source_type,
            brand_requirement=body.brand_requirement,
            supplier_brand_map=supplier_brand_map,
        )
        db.flush()  # 拿到 id
        sessions_out.append({
            "category": cat, "id": s.id, "version": s.version,
            "anchors_total": len(anchors),
        })

    db.commit()

    # 主 session = 锚点最多的品类(向后兼容旧前端读 id/version)
    primary = max(sessions_out, key=lambda x: x["anchors_total"])
    return {
        "ok": True,
        "id": primary["id"],
        "version": primary["version"],
        "sessions": sessions_out,
        "multi_category": len(sessions_out) > 1,
    }


@router.get("/tender-list/current")
def tender_list_current(
    project_id: int | None = Query(None),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    from apps.api.models.tender_list_session import TenderListSession
    q = db.query(TenderListSession).filter(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    session = q.first()
    if not session:
        raise HTTPException(404, "No current TenderListSession found")
    return {
        "id": session.id,
        "version": session.version,
        "category": session.category,
        "file_name": session.file_name,
        "anchors_total": session.anchors_total,
        "status": session.status,
        "confirmed_by": session.confirmed_by,
        "confirmed_at": session.confirmed_at,
        "created_at": session.created_at,
    }


@router.get("/tender-list/versions")
def tender_list_versions(
    project_id: int | None = Query(None),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    from apps.api.models.tender_list_session import TenderListSession
    q = db.query(TenderListSession).filter(TenderListSession.category == category)
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    sessions = q.order_by(TenderListSession.version.desc()).all()
    return [
        {
            "id": s.id, "version": s.version, "is_current": s.is_current,
            "status": s.status, "anchors_total": s.anchors_total,
            "file_name": s.file_name, "created_at": s.created_at,
        }
        for s in sessions
    ]


@router.delete("/tender-list/current")
def tender_list_deactivate(
    project_id: int | None = Query(None),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    """将当前版 is_current=False（保留历史，不删除）。"""
    from apps.api.models.tender_list_session import TenderListSession
    from datetime import datetime as _dt
    q = db.query(TenderListSession).filter(
        TenderListSession.category == category,
        TenderListSession.is_current.is_(True),
    )
    if project_id is not None:
        q = q.filter(TenderListSession.project_id == project_id)
    updated = q.update({"is_current": False, "superseded_at": _dt.utcnow()})
    db.commit()
    return {"ok": True, "deactivated": updated}


# ── 对齐审核闸门 ──────────────────────────────────────────────────

@router.post("/anchor-review/bulk-confirm")
def anchor_review_bulk_confirm(
    project_id: int = Query(...),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    """批量确认所有 pending 对齐项（item 级），将 action='pending' 升为 'align'。"""
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    group_ids = [
        g.id for g in db.query(BidAlignmentGroup.id).filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
        ).all()
    ]
    if not group_ids:
        return {"ok": True, "confirmed": 0}
    updated = (
        db.query(BidAlignmentItem)
        .filter(
            BidAlignmentItem.group_id.in_(group_ids),
            BidAlignmentItem.action == "pending",
        )
        .update({"action": "align"})
    )
    db.commit()
    return {"ok": True, "confirmed": updated}


class _FinalizeBody(BaseModel):
    project_id: int | None = None
    category: str
    force: bool = False
    reason: str = ""
    finalized_by: str = ""


@router.post("/anchor-review/finalize")
def anchor_review_finalize(
    body: _FinalizeBody,
    db: Session = Depends(get_db),
):
    """创建 AlignmentFinalization，锁定当前 confirmed 对齐组快照。

    force=True 时必须提供 reason 字段。
    """
    from apps.api.models.alignment_finalization import AlignmentFinalization
    from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
    from sqlalchemy import func as _func
    from datetime import datetime as _dt

    if body.force and not body.reason:
        raise HTTPException(400, "force=True 时必须提供 reason 字段")

    # v2.5: item-level pending check (group.status is always "confirmed" now)
    pending_count = (
        db.query(_func.count(BidAlignmentItem.id))
        .join(BidAlignmentGroup, BidAlignmentItem.group_id == BidAlignmentGroup.id)
        .filter(
            BidAlignmentGroup.project_id == body.project_id,
            BidAlignmentGroup.category == body.category,
            BidAlignmentItem.action == "pending",
        )
        .scalar() or 0
    )

    if pending_count > 0 and not body.force:
        raise HTTPException(
            409,
            f"仍有 {pending_count} 条 item 处于 pending 状态未处理。"
            "请先逐条确认，或使用 force=true 强制完成（需提供原因）。",
        )

    # Safety gate: refuse finalization if align items still carry valve_type_conflict flag
    fp_align_count = (
        db.query(_func.count(BidAlignmentItem.id))
        .join(BidAlignmentGroup, BidAlignmentItem.group_id == BidAlignmentGroup.id)
        .filter(
            BidAlignmentGroup.project_id == body.project_id,
            BidAlignmentGroup.category == body.category,
            BidAlignmentGroup.status == "confirmed",
            BidAlignmentItem.action == "align",
            BidAlignmentItem.spec_note.like("%valve_type_conflict%"),
        )
        .scalar() or 0
    )
    if fp_align_count > 0 and not body.force:
        raise HTTPException(
            409,
            f"存在 {fp_align_count} 条 align item 含阀型冲突标记，拒绝 finalize。"
            "请重新运行 LLM 填表或使用 force=true 强制完成（需提供原因）。",
        )

    # 锁定当前 confirmed 组的 ID 快照
    confirmed_groups = (
        db.query(BidAlignmentGroup)
        .filter(
            BidAlignmentGroup.project_id == body.project_id,
            BidAlignmentGroup.category == body.category,
            BidAlignmentGroup.status == "confirmed",
        )
        .all()
    )
    group_ids = [g.id for g in confirmed_groups]

    fin = AlignmentFinalization(
        project_id=body.project_id,
        category=body.category,
        group_ids_json=group_ids,
        status="finalized",
        pending_at_finalize=pending_count,
        finalized_by=body.finalized_by or None,
        finalized_at=_dt.utcnow(),
        forced=body.force,
        force_reason=body.reason if body.force else None,
    )
    db.add(fin)
    db.commit()
    db.refresh(fin)
    return {
        "ok": True,
        "id": fin.id,
        "status": "finalized",
        "group_ids_count": len(group_ids),
        "pending_at_finalize": pending_count,
        "forced": body.force,
    }


# ── 比价矩阵闸门 ──────────────────────────────────────────────────

class _BidMatrixSaveBody(BaseModel):
    project_id: int | None = None
    category: str
    alignment_finalization_id: int
    tender_list_session_id: int | None = None
    matrix_json: dict = {}
    readiness_json: list = []
    anchors_count: int = 0
    compared_rows: int = 0
    excluded_rows_json: list = []
    supplier_ids_json: list = []
    recommended_supplier: str = ""


@router.post("/bid-matrix/save")
def bid_matrix_save(
    body: _BidMatrixSaveBody,
    db: Session = Depends(get_db),
):
    """正式保存 BidMatrixVersion。必须有 AlignmentFinalization.status=finalized。"""
    from apps.api.models.alignment_finalization import AlignmentFinalization
    from apps.api.models.bid_matrix_version import BidMatrixVersion

    fin = db.get(AlignmentFinalization, body.alignment_finalization_id)
    if not fin:
        raise HTTPException(404, f"AlignmentFinalization {body.alignment_finalization_id} 不存在")
    if fin.status != "finalized":
        raise HTTPException(
            400,
            f"AlignmentFinalization 状态为 '{fin.status}'，必须为 'finalized' 才能保存矩阵版本",
        )

    # 计算版本号
    last = (
        db.query(BidMatrixVersion)
        .filter(
            BidMatrixVersion.project_id == body.project_id,
            BidMatrixVersion.category == body.category,
        )
        .order_by(BidMatrixVersion.version.desc())
        .first()
    )
    new_version = (last.version + 1) if last else 1

    bmv = BidMatrixVersion(
        project_id=body.project_id,
        category=body.category,
        version=new_version,
        tender_list_session_id=body.tender_list_session_id,
        alignment_finalization_id=body.alignment_finalization_id,
        matrix_json=body.matrix_json,
        readiness_json=body.readiness_json,
        anchors_count=body.anchors_count,
        compared_rows=body.compared_rows,
        excluded_rows_json=body.excluded_rows_json,
        supplier_ids_json=body.supplier_ids_json,
        recommended_supplier=body.recommended_supplier or None,
        status="preview",
    )
    db.add(bmv)
    db.commit()
    db.refresh(bmv)
    return {"ok": True, "id": bmv.id, "version": bmv.version}


@router.get("/bid-matrix/versions")
def bid_matrix_versions(
    project_id: int | None = Query(None),
    category: str = Query(...),
    db: Session = Depends(get_db),
):
    from apps.api.models.bid_matrix_version import BidMatrixVersion
    q = db.query(BidMatrixVersion).filter(BidMatrixVersion.category == category)
    if project_id is not None:
        q = q.filter(BidMatrixVersion.project_id == project_id)
    versions = q.order_by(BidMatrixVersion.version.desc()).all()
    return [
        {
            "id": v.id, "version": v.version, "status": v.status,
            "anchors_count": v.anchors_count, "compared_rows": v.compared_rows,
            "recommended_supplier": v.recommended_supplier,
            "approved_by": v.approved_by, "approved_at": v.approved_at,
            "created_at": v.created_at,
        }
        for v in versions
    ]


@router.get("/bid-matrix/versions/{version_id}")
def bid_matrix_version_get(version_id: int, db: Session = Depends(get_db)):
    from apps.api.models.bid_matrix_version import BidMatrixVersion
    v = db.get(BidMatrixVersion, version_id)
    if not v:
        raise HTTPException(404, f"BidMatrixVersion {version_id} 不存在")
    return {
        "id": v.id, "version": v.version, "status": v.status,
        "project_id": v.project_id, "category": v.category,
        "tender_list_session_id": v.tender_list_session_id,
        "alignment_finalization_id": v.alignment_finalization_id,
        "matrix_json": v.matrix_json,
        "readiness_json": v.readiness_json,
        "anchors_count": v.anchors_count, "compared_rows": v.compared_rows,
        "excluded_rows_json": v.excluded_rows_json,
        "supplier_ids_json": v.supplier_ids_json,
        "recommended_supplier": v.recommended_supplier,
        "review_note": v.review_note,
        "approved_by": v.approved_by, "approved_at": v.approved_at,
        "created_at": v.created_at,
    }


class _ApproveBody(BaseModel):
    note: str = ""
    approved_by: str = ""


@router.post("/bid-matrix/versions/{version_id}/approve")
def bid_matrix_version_approve(
    version_id: int,
    body: _ApproveBody,
    db: Session = Depends(get_db),
):
    from apps.api.models.bid_matrix_version import BidMatrixVersion
    from datetime import datetime as _dt
    v = db.get(BidMatrixVersion, version_id)
    if not v:
        raise HTTPException(404, f"BidMatrixVersion {version_id} 不存在")
    v.status = "approved"
    v.review_note = body.note or None
    v.approved_by = body.approved_by or None
    v.approved_at = _dt.utcnow()
    db.commit()
    return {"ok": True, "id": v.id, "status": "approved"}
