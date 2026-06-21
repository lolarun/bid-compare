"""Bid matrix comparison service — F6.1 横向对比矩阵."""

import re as _re
import string
from dataclasses import dataclass
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, Project, BrandTier
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.extraction_job import ExtractionJob
from apps.api.services.comparison import (
    compute_reasonable_low,
    compute_baseline,
    determine_alert,
    get_category_thresholds,
)


@dataclass
class _ItemData:
    """从 BidAlignmentItem 读取价格/物料数据的统一表示。

    兼容两条路径：
      bid_quote_line_id IS NOT NULL → 从 BidQuoteLine 读取（新路径）
      quote_id IS NOT NULL          → 从 Quote + Material 读取（旧路径）
    """
    unit_price: float | None
    quantity: float | None
    total_price: float | None
    material_id: int | None
    source_quote_id: int | None      # 旧路径: Quote.id；新路径: None
    bid_quote_line_id: int | None    # 新路径: BidQuoteLine.id；旧路径: None
    standard_name: str = ""
    spec: str = ""


def _get_item_data(db: Session, item: BidAlignmentItem) -> "_ItemData | None":
    """从 BidAlignmentItem 读取价格数据，自动选择新/旧路径。

    新路径（bid_quote_line_id IS NOT NULL）：读 BidQuoteLine（独立字段，无需 Material 连接）。
    旧路径（quote_id IS NOT NULL）：读 Quote + Material。
    """
    if item.bid_quote_line_id is not None:
        from apps.api.models.bid_submission import BidQuoteLine
        bql = db.get(BidQuoteLine, item.bid_quote_line_id)
        if bql is None:
            return None
        return _ItemData(
            unit_price=bql.unit_price,
            quantity=bql.qty,
            total_price=bql.total_price,
            material_id=bql.material_id,
            source_quote_id=None,
            bid_quote_line_id=bql.id,
            standard_name=bql.standard_name,
            spec=bql.spec or "",
        )
    else:
        qt = db.get(Quote, item.quote_id)
        if qt is None:
            return None
        mat = db.get(Material, qt.material_id) if qt.material_id else None
        return _ItemData(
            unit_price=qt.unit_price,
            quantity=qt.quantity,
            total_price=qt.total_price,
            material_id=qt.material_id,
            source_quote_id=qt.id,
            bid_quote_line_id=None,
            standard_name=mat.standard_name if mat else "",
            spec=(mat.spec or "") if mat else "",
        )

# Cell status constants
CELL_QUOTED = "quoted"        # confirmed align item with price
CELL_AGGREGATED = "aggregated"  # aggregated multi-row align item
CELL_PENDING = "pending"      # pending item — show price in orange, exclude from calcs
CELL_EXCLUDED = "excluded"    # explicitly excluded
CELL_MISSING = "missing"      # no item at all (supplier didn't quote)


def _get_supplier_checksum(db: Session, supplier_id: int, project_id: int | None) -> dict:
    """Return checksum dict from the most recent ExtractionJob for this supplier+project."""
    q = db.query(Quote.batch_id).filter(Quote.supplier_id == supplier_id)
    if project_id:
        q = q.filter(Quote.project_id == project_id)
    batch_ids = [r[0] for r in q.distinct().all() if r[0]]
    if not batch_ids:
        return {}
    job = (
        db.query(ExtractionJob)
        .filter(ExtractionJob.id.in_(batch_ids))
        .order_by(ExtractionJob.created_at.desc())
        .first()
    )
    return ((job.result or {}).get("_checksum") or {}) if job else {}


def _get_submission_checksum(db: Session, sub) -> dict:
    """Return checksum dict from the ExtractionJob linked to a BidSubmission."""
    job = db.get(ExtractionJob, sub.job_id) if sub.job_id else None
    return ((job.result or {}).get("_checksum") or {}) if job else {}


def _detect_brand_tier_filter(
    db: Session,
    supplier_ids: list[int],
    category: str,
    project_id: int | None,
) -> str | None:
    """For single-supplier comparison, detect whether to filter baselines by brand tier.

    Rule: if ALL quotes from this supplier have JV (合资) brands,
    compare only against JV historical prices. Otherwise compare against all.
    Returns '合资' or None.
    """
    if len(supplier_ids) != 1:
        return None

    q = db.query(Quote.brand_tier).join(Material).filter(
        Quote.supplier_id == supplier_ids[0],
        Material.category == category,
        Quote.unit_price > 0,
    )
    if project_id:
        q = q.filter(Quote.project_id == project_id)

    tiers = {r[0] for r in q.all() if r[0]}
    if not tiers:
        return None
    if tiers == {"合资"}:
        return "合资"
    return None


def _compute_row_baselines(
    db: Session, cat: str, sub_cat: str | None, tier_filter: str | None,
) -> tuple[dict | None, dict | None]:
    """Return (historical_avg_info, reasonable_low_info) dicts for a category/sub."""
    rl = compute_reasonable_low(db, cat, sub_cat, brand_tier=tier_filter)
    baseline = compute_baseline(db, cat, sub_cat, brand_tier=tier_filter)

    reasonable_low_price = rl.get("reasonable_low")
    hist_avg = baseline.get("mean")
    sample_count = baseline.get("count", 0)

    historical_avg_info = None
    if hist_avg and sample_count > 0:
        dates_q = db.query(Quote.quote_date).join(Material).filter(
            Material.category == cat,
            Quote.quote_date != "",
            Quote.quote_date.isnot(None),
        ).all()
        dates = sorted([r[0] for r in dates_q if r[0]])
        period = f"{dates[0]}~{dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "")
        projects_count = db.query(Quote.project_id).join(Material).filter(
            Material.category == cat,
            Quote.project_id.isnot(None),
        ).distinct().count()
        historical_avg_info = {
            "price": round(hist_avg, 2),
            "period": period,
            "projects": projects_count,
        }

    reasonable_low_info = None
    if reasonable_low_price:
        reasonable_low_info = {
            "price": round(reasonable_low_price, 2),
            "date": rl.get("reasonable_low_date") or "",
            "project": rl.get("reasonable_low_project") or "",
            # 标记是否来自全品类聚合（sub_cat 为空时偏差基准不可靠）
            "broad_baseline": sub_cat is None,
        }

    return historical_avg_info, reasonable_low_info


def _finalize_row(
    supplier_cells: list[dict],
    prices_this_row: list[tuple],
    letter_map: dict[int, str],
) -> tuple[float | None, str | None]:
    """Mark lowest price and compute min deviation / recommended supplier."""
    if prices_this_row:
        min_price_val = min(p for _, p, _ in prices_this_row)
        for cell in supplier_cells:
            if cell["price"] == min_price_val:
                cell["is_lowest"] = True
                break

    deviations_with_sid = [(sid, dev) for sid, _, dev in prices_this_row if dev is not None]
    min_deviation = None
    recommended = None
    if deviations_with_sid:
        best = min(deviations_with_sid, key=lambda x: x[1])
        min_deviation = round(best[1], 4)
        recommended = letter_map.get(best[0])
    return min_deviation, recommended


def _build_material_row(
    db: Session,
    mat: Material,
    supplier_ids: list[int],
    project_id: int | None,
    tier_filter: str | None,
    letter_map: dict[int, str],
    aligned_quote_ids: set[int] | None = None,
) -> dict | None:
    """Build a matrix row for a single material (original, non-aligned path)."""
    mat_category = mat.category

    historical_avg, reasonable_low_info = _compute_row_baselines(
        db, mat_category, mat.sub_category or None, tier_filter,
    )
    thresholds = get_category_thresholds(db, mat_category)
    reasonable_low_price = reasonable_low_info["price"] if reasonable_low_info else None

    supplier_cells = []
    prices_this_row = []

    for sid in supplier_ids:
        quote = db.query(Quote).filter(
            Quote.material_id == mat.id,
            Quote.supplier_id == sid,
            Quote.unit_price > 0,
        )
        if project_id:
            quote = quote.filter(Quote.project_id == project_id)
        qt = quote.order_by(Quote.id.desc()).first()

        # Skip if this quote is already handled by an alignment group
        if qt and aligned_quote_ids and qt.id in aligned_quote_ids:
            qt = None

        if qt:
            price = qt.unit_price
            qty = qt.quantity or 1
            total = round(price * qty, 2) if price else None
            dev = round((price - reasonable_low_price) / reasonable_low_price, 4) if reasonable_low_price else None
            alert = determine_alert(dev, thresholds) if dev is not None else "normal"
            prices_this_row.append((sid, price, dev))
            supplier_cells.append({
                "supplier_id": sid,
                "price": price,
                "total": total,
                "deviation_pct": dev,
                "alert_level": alert,
                "is_lowest": False,
            })
        else:
            supplier_cells.append({
                "supplier_id": sid,
                "price": None,
                "total": None,
                "deviation_pct": None,
                "alert_level": "normal",
                "is_lowest": False,
            })

    # Skip row if no supplier has a quote (all aligned away)
    if not prices_this_row:
        return None

    min_deviation, recommended = _finalize_row(supplier_cells, prices_this_row, letter_map)

    return {
        "material_id": mat.id,
        "material_name": mat.standard_name,
        "spec": mat.spec or "",
        "historical_avg": historical_avg,
        "reasonable_low": reasonable_low_info,
        "suppliers": supplier_cells,
        "min_deviation": min_deviation,
        "recommended": recommended,
    }


def _build_alignment_row(
    db: Session,
    ag: BidAlignmentGroup,
    supplier_ids: list[int],
    project_id: int | None,
    tier_filter: str | None,
    letter_map: dict[int, str],
) -> dict | None:
    """Build a matrix row from an alignment group (AI-confirmed grouping)."""
    # Build lookup: supplier_id → (item, _ItemData)
    item_by_supplier: dict[int, tuple] = {}
    for item in ag.items:
        if item.action != "align":
            continue
        data = _get_item_data(db, item)
        if data and data.unit_price and data.unit_price > 0:
            sid = item.supplier_id
            existing = item_by_supplier.get(sid)
            if existing is None or data.unit_price < existing[1].unit_price:
                item_by_supplier[sid] = (item, data)

    if not item_by_supplier:
        return None

    # Use the first aligned item's material for baseline lookups
    first_data = next(iter(item_by_supplier.values()))[1]
    mat = db.get(Material, first_data.material_id) if first_data.material_id else None
    mat_category = mat.category if mat else ag.category

    historical_avg, reasonable_low_info = _compute_row_baselines(
        db, mat_category, mat.sub_category if mat else None, tier_filter,
    )
    thresholds = get_category_thresholds(db, mat_category)
    reasonable_low_price = reasonable_low_info["price"] if reasonable_low_info else None

    supplier_cells = []
    prices_this_row = []

    for sid in supplier_ids:
        pair = item_by_supplier.get(sid)
        if pair:
            item, qt = pair
            # Use aggregated pricing when available (multi-row same-canonical aggregation)
            if item.agg_total is not None and item.agg_qty:
                price = round(item.agg_total / item.agg_qty, 4)
                total = round(item.agg_total, 2)
            else:
                price = qt.unit_price
                total = round(price * (qt.quantity or 1), 2) if price else None
            dev = round((price - reasonable_low_price) / reasonable_low_price, 4) if reasonable_low_price else None
            alert = determine_alert(dev, thresholds) if dev is not None else "normal"
            prices_this_row.append((sid, price, dev))
            supplier_cells.append({
                "supplier_id": sid,
                "price": price,
                "total": total,
                "deviation_pct": dev,
                "alert_level": alert,
                "is_lowest": False,
            })
        else:
            supplier_cells.append({
                "supplier_id": sid,
                "price": None,
                "total": None,
                "deviation_pct": None,
                "alert_level": "normal",
                "is_lowest": False,
            })

    min_deviation, recommended = _finalize_row(supplier_cells, prices_this_row, letter_map)

    return {
        "material_id": first_qt.material_id,  # reference material
        "material_name": ag.suggested_name,  # use aligned name
        "spec": ag.suggested_spec,  # use aligned spec
        "historical_avg": historical_avg,
        "reasonable_low": reasonable_low_info,
        "suppliers": supplier_cells,
        "min_deviation": min_deviation,
        "recommended": recommended,
    }


def build_bid_matrix(
    db: Session,
    supplier_ids: list[int],
    project_id: int | None = None,
    material_ids: list[int] | None = None,
    category: str | None = None,
    allowed_group_ids: set[int] | None = None,
) -> dict:
    """Build the horizontal bid comparison matrix.

    Args:
        supplier_ids: 参与比价的供应商 ID 列表
        project_id: 当前比价项目（可选）
        material_ids: 限定物料范围（可选，None=全部）
        category: 限定品类（可选）
    """
    # ── 1. 确定供应商标签 (A/B/C/D...) ────────────────────────────────────
    letters = list(string.ascii_uppercase)
    supplier_labels = []
    for i, sid in enumerate(supplier_ids):
        sup = db.get(Supplier, sid)
        if sup:
            supplier_labels.append({
                "id": sid,
                "letter": letters[i] if i < len(letters) else str(i + 1),
                "name": sup.name,
            })
    letter_map = {sl["id"]: sl["letter"] for sl in supplier_labels}

    # ── 2. 确定需要比价的物料列表 ────────────────────────────────────────
    if material_ids:
        materials = [db.get(Material, mid) for mid in material_ids if db.get(Material, mid)]
    else:
        # 从这些供应商在该项目的报价中获取物料
        q = db.query(Quote.material_id).filter(
            Quote.supplier_id.in_(supplier_ids),
            Quote.unit_price > 0,
        )
        if project_id:
            q = q.filter(Quote.project_id == project_id)
        if category:
            q = q.join(Material).filter(Material.category == category)
        mid_set = {r[0] for r in q.distinct().all()}
        materials = [db.get(Material, mid) for mid in mid_set if db.get(Material, mid)]

    if not materials:
        return {
            "project_id": project_id,
            "suppliers": supplier_labels,
            "rows": [],
            "totals": [],
        }

    # ── 2.5 Brand-tier-aware baseline for single-supplier comparison ──────
    tier_filter = _detect_brand_tier_filter(db, supplier_ids, category or "", project_id)

    # ── 2.6 Load confirmed alignment groups ─────────────────────────────
    alignment_groups: list[BidAlignmentGroup] = []
    aligned_quote_ids: set[int] = set()
    if project_id and category:
        q = db.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
        )
        if allowed_group_ids is not None:
            q = q.filter(BidAlignmentGroup.id.in_(allowed_group_ids))
        alignment_groups = q.all()
        for ag in alignment_groups:
            for item in ag.items:
                if item.action == "align" and item.quote_id is not None:
                    aligned_quote_ids.add(item.quote_id)

    # ── 3. 按物料构建矩阵行 ───────────────────────────────────────────────
    rows = []

    # 3a. Rows from alignment groups (take priority)
    for ag in alignment_groups:
        row = _build_alignment_row(
            db, ag, supplier_ids, project_id, tier_filter, letter_map,
        )
        if row:
            rows.append(row)

    # 3b. Rows from unaligned materials (skip quotes already in alignment groups)
    for mat in materials:
        row = _build_material_row(
            db, mat, supplier_ids, project_id, tier_filter, letter_map,
            aligned_quote_ids=aligned_quote_ids,
        )
        if row:
            rows.append(row)

    # ── 4. 汇总 totals ────────────────────────────────────────────────────
    supplier_totals: dict[int, dict] = {
        sid: {"total": 0.0, "devs": [], "quoted": 0, "anomalies": 0}
        for sid in supplier_ids
    }
    for row in rows:
        for cell in row["suppliers"]:
            sid = cell["supplier_id"]
            if cell["price"] is not None:
                supplier_totals[sid]["quoted"] += 1
            if cell["total"] is not None:
                supplier_totals[sid]["total"] += cell["total"]
            if cell["deviation_pct"] is not None:
                supplier_totals[sid]["devs"].append(cell["deviation_pct"])
            if cell["alert_level"] == "red":
                supplier_totals[sid]["anomalies"] += 1

    totals = []
    for sid in supplier_ids:
        data = supplier_totals[sid]
        avg_dev = sum(data["devs"]) / len(data["devs"]) if data["devs"] else 0.0
        cs = _get_supplier_checksum(db, sid, project_id)
        totals.append({
            "supplier_id": sid,
            "total": round(data["total"], 2),
            "avg_deviation": round(avg_dev, 4),
            "quoted_count": data["quoted"],
            "anomaly_count": data["anomalies"],
            "declared_total": cs.get("declared"),
            "checksum_delta_pct": cs.get("delta_pct"),
            "checksum_status": cs.get("status"),
        })

    return {
        "project_id": project_id,
        "suppliers": supplier_labels,
        "rows": rows,
        "totals": totals,
        "brand_tier_filter": tier_filter,
    }


def _parse_cosine_from_note(spec_note: str | None) -> float | None:
    """Extract 'cos=0.XX' from spec_note string."""
    if not spec_note:
        return None
    m = _re.search(r"cos=(\d+\.?\d*)", spec_note)
    return float(m.group(1)) if m else None


def _parse_flags_from_note(spec_note: str | None) -> list[str]:
    """Extract flags from spec_note — everything after 'cos=X.XX '."""
    if not spec_note:
        return []
    m = _re.search(r"cos=\d+\.?\d*\s+(.*)", spec_note)
    if not m:
        return []
    return [f for f in m.group(1).split(",") if f.strip()]


def _build_cell_for_supplier(
    db: Session,
    items: list[BidAlignmentItem],
    sid: int,
    reasonable_low_price: float | None,
    thresholds: dict,
    letter_map: dict[int, str],
) -> dict:
    """Build a SupplierCell dict for one (group, supplier) combination.

    Priority: align/aggregated > pending > excluded > missing
    Pending prices are shown (method A) but excluded from totals/lowest/recommended.
    """
    align_items = [i for i in items if i.action == "align"]
    pending_items = [i for i in items if i.action == "pending"]
    excluded_items = [i for i in items if i.action == "exclude"]

    base = {
        "supplier_id": sid,
        "price": None,
        "total": None,
        "deviation_pct": None,
        "alert_level": "normal",
        "is_lowest": False,
        # Extended cell info
        "cell_status": CELL_MISSING,
        "item_id": None,
        "confidence": None,
        "source_quote_id": None,
        "bid_quote_line_id": None,
        "pending_note": None,
        "flags": None,
        "evidence": None,
    }

    def _price_from_item(item: BidAlignmentItem) -> tuple[float | None, float | None, int | None, int | None]:
        """Return (unit_price, total, source_quote_id, bid_quote_line_id)."""
        data = _get_item_data(db, item)
        if not data:
            return None, None, None, None
        if item.agg_total is not None and item.agg_qty:
            price = round(item.agg_total / item.agg_qty, 4)
            total = round(item.agg_total, 2)
        else:
            price = data.unit_price
            total = round(price * (data.quantity or 1), 2) if price else None
        return price, total, data.source_quote_id, data.bid_quote_line_id

    def _fill_price(cell: dict, price: float | None, total: float | None,
                    source_qid: int | None, bql_id: int | None = None) -> None:
        cell["price"] = price
        cell["total"] = total
        cell["source_quote_id"] = source_qid
        cell["bid_quote_line_id"] = bql_id
        if price and reasonable_low_price:
            dev = round((price - reasonable_low_price) / reasonable_low_price, 4)
            cell["deviation_pct"] = dev
            cell["alert_level"] = determine_alert(dev, thresholds) if dev is not None else "normal"

    if align_items:
        # Pick best align item: prefer aggregated; among multiple, take lowest effective price
        def _effective_price(i: BidAlignmentItem) -> float:
            if i.agg_total is not None and i.agg_qty:
                return i.agg_total / i.agg_qty
            data = _get_item_data(db, i)
            return (data.unit_price or float("inf")) if data else float("inf")

        best = min(align_items, key=_effective_price)
        price, total, source_qid, bql_id = _price_from_item(best)
        _fill_price(base, price, total, source_qid, bql_id)
        # Use AGGREGATED if agg columns are set, else QUOTED
        base["cell_status"] = CELL_AGGREGATED if (best.agg_total is not None) else CELL_QUOTED
        base["flags"] = _parse_flags_from_note(best.spec_note) or None
        base["evidence"] = best.name_note or None
        # Annotate if there are also pending items (inline action hint)
        if pending_items:
            base["pending_note"] = f"另有 {len(pending_items)} 条待确认"
        return base

    if pending_items:
        # Show reference price in orange; don't count in totals
        best = max(pending_items, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0)
        price, total, source_qid, bql_id = _price_from_item(best)
        _fill_price(base, price, total, source_qid, bql_id)
        base["cell_status"] = CELL_PENDING
        base["item_id"] = best.id
        base["confidence"] = _parse_cosine_from_note(best.spec_note)
        base["flags"] = _parse_flags_from_note(best.spec_note) or None
        base["evidence"] = best.name_note or None
        return base

    if excluded_items:
        base["cell_status"] = CELL_EXCLUDED
        return base

    # missing — no item for this supplier
    return base


def build_anchor_matrix(
    db: Session,
    anchors: list,  # list[TenderAnchor] — avoid circular import; duck-typed
    tender_list_session_id: int | None,
    supplier_ids: list[int],
    project_id: int | None,
    category: str,
    allowed_group_ids: set[int] | None = None,
    used_submission_ids: list[int] | None = None,
    submission_ids: list[int] | None = None,
) -> dict:
    """Build the bid matrix anchored to ALL tender list items (v2.5).

    Every anchor becomes exactly one matrix row regardless of whether suppliers quoted it.
    Cell statuses: quoted / aggregated / pending / excluded / missing.
    Pending cells show reference price (method A) but are excluded from:
      - is_lowest calculation
      - supplier total price
      - avg_deviation
      - recommended supplier

    Column mode (v4.0):
    - When used_submission_ids or submission_ids is non-empty: columns keyed by BidSubmission.id.
      Supports unknown suppliers (supplier_id nullable). Column "id" = submission_id.
    - Otherwise: fallback to supplier_id columns (backward compat for LLM fill internal call).
    """
    from apps.api.models.bid_submission import BidSubmission

    letters = list(string.ascii_uppercase)

    # ── Determine column basis: submission-based or supplier-based ────────────
    _all_sub_ids = list(
        dict.fromkeys((used_submission_ids or []) + (submission_ids or []))
    )
    use_submission_mode = bool(_all_sub_ids)

    if use_submission_mode:
        # Load BidSubmission records in the given order, dedup by id
        subs: list[BidSubmission] = []
        seen_sub_ids: set[int] = set()
        for sub_id in _all_sub_ids:
            if sub_id not in seen_sub_ids:
                sub = db.get(BidSubmission, sub_id)
                if sub:
                    subs.append(sub)
                    seen_sub_ids.add(sub_id)

        supplier_labels = []
        for i, sub in enumerate(subs):
            # Display name: linked Supplier.name if available, else raw name from submission
            if sub.supplier_id:
                sup = db.get(Supplier, sub.supplier_id)
                display_name = sup.name if sup else sub.supplier_raw_name
            else:
                display_name = sub.supplier_raw_name
            supplier_labels.append({
                "id": sub.id,          # column key = submission_id
                "letter": letters[i] if i < len(letters) else str(i + 1),
                "name": display_name,
                "supplier_id": sub.supplier_id,  # actual FK (may be None)
            })
        # col_ids is submission_ids; letter_map keyed by submission_id
        col_ids = [s.id for s in subs]
        letter_map = {sl["id"]: sl["letter"] for sl in supplier_labels}
        # Build item filter: prefer submission_id match, fallback to supplier_id for old items
        def _items_for_col(items, col_id: int, actual_supplier_id):
            return [
                i for i in items
                if i.submission_id == col_id
                or (i.submission_id is None and actual_supplier_id and i.supplier_id == actual_supplier_id)
            ]
        sub_actual_sids = {sub.id: sub.supplier_id for sub in subs}
    else:
        # Legacy supplier-id columns
        supplier_labels = []
        for i, sid in enumerate(supplier_ids):
            sup = db.get(Supplier, sid)
            if sup:
                supplier_labels.append({
                    "id": sid,
                    "letter": letters[i] if i < len(letters) else str(i + 1),
                    "name": sup.name,
                })
        col_ids = [sl["id"] for sl in supplier_labels]
        letter_map = {sl["id"]: sl["letter"] for sl in supplier_labels}
        def _items_for_col(items, col_id: int, actual_supplier_id):
            return [i for i in items if i.supplier_id == col_id]
        sub_actual_sids = {}

    # ── Load groups: when session is known, ONLY load that session's groups ──
    # Prevents cross-session contamination when re-running match after a bad round.
    q = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
        BidAlignmentGroup.status == "confirmed",
    )
    if tender_list_session_id is not None:
        # Strict: only groups that were created by this specific session
        q = q.filter(BidAlignmentGroup.tender_list_session_id == tender_list_session_id)
    if allowed_group_ids is not None:
        q = q.filter(BidAlignmentGroup.id.in_(allowed_group_ids))
    all_groups: list[BidAlignmentGroup] = q.all()

    # Build lookup: anchor_seq → group (no fallback needed — session already filtered)
    seq_to_group: dict[str, BidAlignmentGroup] = {}
    for g in all_groups:
        if g.anchor_seq is None:
            continue
        seq = str(g.anchor_seq)
        if seq not in seq_to_group:
            seq_to_group[seq] = g

    tier_filter = _detect_brand_tier_filter(db, supplier_ids, category, project_id)

    # ── Build one row per anchor ─────────────────────────────────────────────
    rows = []
    supplier_totals: dict[int, dict] = {
        col_id: {"total": 0.0, "devs": [], "quoted": 0, "anomalies": 0}
        for col_id in col_ids
    }

    for anchor in anchors:
        seq_key = str(anchor.seq)
        group = seq_to_group.get(seq_key)

        # Determine baseline for this anchor's category
        if group:
            # Use the first align item's material for category lookup
            first_align = next(
                (i for i in group.items if i.action == "align"), None
            )
            if first_align:
                _align_data = _get_item_data(db, first_align)
                _mid = _align_data.material_id if _align_data else None
                ref_mat = db.get(Material, _mid) if _mid else None
            else:
                ref_mat = None
            mat_category = ref_mat.category if ref_mat else category
            sub_cat = ref_mat.sub_category if ref_mat else None
        else:
            mat_category = category
            sub_cat = None

        historical_avg, reasonable_low_info = _compute_row_baselines(
            db, mat_category, sub_cat, tier_filter
        )
        thresholds = get_category_thresholds(db, mat_category)
        # 仅当 sub_cat 明确时使用基准价（全品类地板价粒度过粗，偏差无意义）
        reasonable_low_price = (
            reasonable_low_info["price"]
            if reasonable_low_info and not reasonable_low_info.get("broad_baseline")
            else None
        )

        # ── Build cells for each column (submission or supplier) ──────────
        supplier_cells: list[dict] = []
        prices_this_row: list[tuple] = []  # only quoted/aggregated

        for col_id in col_ids:
            actual_sid = sub_actual_sids.get(col_id, col_id) if use_submission_mode else col_id
            if group is None:
                # No match at all — missing for all suppliers
                cell = {
                    "supplier_id": col_id,
                    "price": None,
                    "total": None,
                    "deviation_pct": None,
                    "alert_level": "normal",
                    "is_lowest": False,
                    "cell_status": CELL_MISSING,
                    "item_id": None,
                    "confidence": None,
                    "source_quote_id": None,
                    "bid_quote_line_id": None,
                    "pending_note": None,
                }
            else:
                col_items = _items_for_col(group.items, col_id, actual_sid)
                cell = _build_cell_for_supplier(
                    db, col_items, col_id,
                    reasonable_low_price, thresholds, letter_map,
                )

            supplier_cells.append(cell)

            # Only confirmed cells participate in lowest/totals
            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED) and cell["price"] is not None:
                prices_this_row.append((col_id, cell["price"], cell.get("deviation_pct")))
                supplier_totals[col_id]["quoted"] += 1
                if cell["total"] is not None:
                    supplier_totals[col_id]["total"] += cell["total"]
                if cell["deviation_pct"] is not None:
                    supplier_totals[col_id]["devs"].append(cell["deviation_pct"])
            if cell.get("alert_level") == "red":
                supplier_totals[col_id]["anomalies"] += 1

        # Mark lowest price (among quoted/aggregated only)
        min_deviation, recommended = _finalize_row(supplier_cells, prices_this_row, letter_map)

        # Use first confirmed item's material_id as row reference
        ref_material_id: int | None = None
        if group:
            for it in group.items:
                if it.action == "align":
                    _ref_data = _get_item_data(db, it)
                    if _ref_data:
                        ref_material_id = _ref_data.material_id
                        break

        rows.append({
            "material_id": ref_material_id,
            "material_name": anchor.name,
            "spec": getattr(anchor, "spec", "") or "",
            "materials": anchor.material_text() if hasattr(anchor, "material_text") else "",
            "brand": getattr(anchor, "brand", "") or "",
            "anchor_seq": str(anchor.seq),
            "historical_avg": historical_avg,
            "reasonable_low": reasonable_low_info,
            "suppliers": supplier_cells,
            "min_deviation": min_deviation,
            "recommended": recommended,
        })

    # ── Totals (quoted-only cells) ────────────────────────────────────────────
    totals = []
    recommendation_blocked_reasons: list[str] = []
    for col_id in col_ids:
        data = supplier_totals[col_id]
        # avg_deviation=null when 0 quotes (0.0 would falsely make them "best")
        avg_dev: float | None = (
            round(sum(data["devs"]) / len(data["devs"]), 4)
            if data["devs"] else None
        )
        if use_submission_mode:
            sub = db.get(BidSubmission, col_id)
            cs = _get_submission_checksum(db, sub) if sub else {}
        else:
            cs = _get_supplier_checksum(db, col_id, project_id)
        quoted = data["quoted"]
        anomalies = data["anomalies"]
        checksum_status = cs.get("status", "unknown")
        col_label = next((sl["name"] for sl in supplier_labels if sl["id"] == col_id), str(col_id))
        # Per-supplier blocking conditions
        if quoted == 0:
            recommendation_blocked_reasons.append(f"{col_label} 无有效报价")
        if checksum_status in ("fail", "unknown"):
            cs_label = "核验金额不符" if checksum_status == "fail" else "无核验金额"
            recommendation_blocked_reasons.append(f"{col_label} {cs_label}")
        if anomalies > 0:
            recommendation_blocked_reasons.append(f"{col_label} 含 {anomalies} 个异常价格")
        totals.append({
            "supplier_id": col_id,
            "total": round(data["total"], 2),
            "avg_deviation": avg_dev,
            "quoted_count": quoted,
            "anomaly_count": anomalies,
            "declared_total": cs.get("declared"),
            "checksum_delta_pct": cs.get("delta_pct"),
            "checksum_status": checksum_status,
        })

    # Check if any row has pending cells
    pending_count = sum(
        1 for row in rows
        for cell in row.get("suppliers", [])
        if cell.get("cell_status") == "pending"
    )
    if pending_count > 0:
        recommendation_blocked_reasons.append(f"{pending_count} 个存在待确认报价单元格")

    # Check if no baseline is available at all (broad_baseline for all rows → no deviation)
    all_devs_empty = all(not supplier_totals[col_id]["devs"] for col_id in col_ids)
    if all_devs_empty and col_ids:
        recommendation_blocked_reasons.append("缺少同规格历史基准（无法计算偏差）")

    # Completeness threshold: supplier must quote ≥50% of anchors
    total_anchors = len(anchors)
    COMPLETENESS_THRESHOLD = 0.5
    if total_anchors > 0:
        for col_id in col_ids:
            quoted = supplier_totals[col_id]["quoted"]
            ratio = quoted / total_anchors
            if ratio < COMPLETENESS_THRESHOLD:
                col_label = next((sl["name"] for sl in supplier_labels if sl["id"] == col_id), str(col_id))
                recommendation_blocked_reasons.append(
                    f"{col_label} 报价完整度不足（{quoted}/{total_anchors}="
                    f"{int(ratio*100)}%，要求≥{int(COMPLETENESS_THRESHOLD*100)}%）"
                )

    # When recommendation is blocked, null out all per-row recommended fields
    if recommendation_blocked_reasons:
        for row in rows:
            row["recommended"] = None

    from apps.api.services.matrix_stats import build_matrix_distribution_from_rows
    matrix_distribution = build_matrix_distribution_from_rows(rows, col_ids)

    return {
        "project_id": project_id,
        "suppliers": supplier_labels,
        "rows": rows,
        "totals": totals,
        "brand_tier_filter": tier_filter,
        "anchor_matrix": True,  # flag for frontend to know it's anchor-driven
        "matrix_distribution": matrix_distribution,
        "recommendation_blocked": bool(recommendation_blocked_reasons),
        "recommendation_blocked_reasons": recommendation_blocked_reasons,
    }


# ─── Anchor Review Matrix ──────────────────────────────────────────────────────

def _build_review_cell(db: Session, items: list[BidAlignmentItem], sid: int) -> dict:
    """Build a review cell for the anchor-review matrix UI.

    Simpler than _build_cell_for_supplier: no deviation/alert, but adds candidates.
    """
    align_items = [i for i in items if i.action == "align"]
    pending_items = [i for i in items if i.action == "pending"]
    excluded_items = [i for i in items if i.action == "exclude"]

    base: dict = {
        "cell_status": CELL_MISSING,
        "item_id": None,
        "quote_id": None,
        "bid_quote_line_id": None,
        "unit_price": None,
        "total_price": None,
        "confidence": None,
        "evidence": None,
        "flags": None,
        "is_lowest": False,
        "candidates": [],
    }

    def _get_prices(item: BidAlignmentItem) -> tuple:
        """Return (unit_price, total, source_quote_id, bid_quote_line_id)."""
        data = _get_item_data(db, item)
        if not data:
            return None, None, None, None
        if item.agg_total is not None and item.agg_qty:
            price = round(item.agg_total / item.agg_qty, 4)
            total = round(item.agg_total, 2)
        else:
            price = data.unit_price
            total = round(price * (data.quantity or 1), 2) if price else None
        return price, total, data.source_quote_id, data.bid_quote_line_id

    def _build_candidates(plist: list) -> list:
        out = []
        for item in sorted(plist, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0, reverse=True)[:5]:
            data = _get_item_data(db, item)
            if not data:
                continue
            out.append({
                "item_id": item.id,
                "quote_id": data.source_quote_id,
                "bid_quote_line_id": data.bid_quote_line_id,
                "material_name": data.standard_name,
                "spec": data.spec,
                "unit_price": data.unit_price,
                "confidence": _parse_cosine_from_note(item.spec_note),
                "flags": _parse_flags_from_note(item.spec_note) or None,
            })
        return out

    if align_items:
        def _eff(i: BidAlignmentItem) -> float:
            if i.agg_total is not None and i.agg_qty:
                return i.agg_total / i.agg_qty
            data = _get_item_data(db, i)
            return (data.unit_price or float("inf")) if data else float("inf")

        best = min(align_items, key=_eff)
        price, total, source_qid, bql_id = _get_prices(best)
        base.update({
            "cell_status": CELL_AGGREGATED if best.agg_total is not None else CELL_QUOTED,
            "item_id": best.id,
            "quote_id": source_qid,
            "bid_quote_line_id": bql_id,
            "unit_price": price,
            "total_price": total,
            "evidence": best.name_note or None,
            "flags": _parse_flags_from_note(best.spec_note) or None,
            "candidates": _build_candidates(pending_items),
        })
        return base

    if pending_items:
        best = max(pending_items, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0)
        price, total, source_qid, bql_id = _get_prices(best)
        base.update({
            "cell_status": CELL_PENDING,
            "item_id": best.id,
            "quote_id": source_qid,
            "bid_quote_line_id": bql_id,
            "unit_price": price,
            "total_price": total,
            "confidence": _parse_cosine_from_note(best.spec_note),
            "evidence": best.name_note or None,
            "flags": _parse_flags_from_note(best.spec_note) or None,
            "candidates": _build_candidates(pending_items),
        })
        return base

    if excluded_items:
        base["cell_status"] = CELL_EXCLUDED
        return base

    return base  # missing


def build_anchor_review_matrix(
    db: Session,
    project_id: int,
    category: str,
    supplier_ids: list[int] | None = None,
) -> dict:
    """Anchor-first review matrix for the pre-review UI.

    Returns one row per TenderAnchor with cells dict keyed by str(supplier_id).
    Includes candidates list for pending cells. Does not compute deviations or
    alert levels (those are for the final bid matrix).

    supplier_ids: 本次比价的供应商集合。若提供，矩阵列严格等于该列表，不自动扫历史报价。
    """
    from apps.api.models.tender_list_session import TenderListSession
    from apps.api.services.tender_list import rebuild_anchors

    session = (
        db.query(TenderListSession)
        .filter(
            TenderListSession.project_id == project_id,
            TenderListSession.category == category,
            TenderListSession.is_current == True,  # noqa: E712
            TenderListSession.status == "confirmed",
        )
        .first()
    )
    if not session:
        raise ValueError(f"No current TenderListSession for project {project_id} / {category}")

    anchors = rebuild_anchors(session)

    if not supplier_ids:
        raise ValueError(
            "supplier_ids 不可为空 — 比价流程禁止扫历史全量供应商。"
            "请先完成供应商报价上传并「开始匹配」后再调用本接口。"
        )
    supplier_ids = sorted(set(supplier_ids))

    # 供应商参与品牌（招标文件第13页）：supplier_id → brand（品牌作为供应商属性展示，绝不替代供应商列）
    supplier_brand: dict[int, str] = {}
    for sb in (session.supplier_brand_map or []):
        if isinstance(sb, dict) and sb.get("supplier_id") is not None and sb.get("brand"):
            supplier_brand[int(sb["supplier_id"])] = str(sb["brand"])

    # Supplier info + checksum
    suppliers_info = []
    for sid in supplier_ids:
        sup = db.get(Supplier, sid)
        if not sup:
            continue
        cs = _get_supplier_checksum(db, sid, project_id)
        suppliers_info.append({
            "supplier_id": sid,
            "supplier_name": sup.name,
            "brand": supplier_brand.get(sid, ""),
            "checksum_status": cs.get("status"),
            "declared_total": cs.get("declared"),
            "checksum_delta_pct": cs.get("delta_pct"),
        })

    # Load confirmed groups scoped to current session ONLY (prevents historical data leakage)
    all_groups = (
        db.query(BidAlignmentGroup)
        .filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
            BidAlignmentGroup.tender_list_session_id == session.id,
        )
        .all()
    )
    seq_to_group: dict[str, BidAlignmentGroup] = {}
    for g in all_groups:
        if g.anchor_seq is None:
            continue
        seq = str(g.anchor_seq)
        if seq not in seq_to_group:
            seq_to_group[seq] = g

    # Build rows
    rows = []
    pending_cells = 0
    missing_cells = 0
    quoted_ge_2 = 0
    quoted_full = 0
    n = len(supplier_ids)

    for anchor in anchors:
        seq_key = str(anchor.seq)
        group = seq_to_group.get(seq_key)

        cells: dict[str, dict] = {}
        quoted_count = 0
        covered_count = 0
        prices_this_row: dict[int, float] = {}

        for sid in supplier_ids:
            if group is None:
                cell: dict = {
                    "cell_status": CELL_MISSING,
                    "item_id": None,
                    "quote_id": None,
                    "unit_price": None,
                    "total_price": None,
                    "confidence": None,
                    "evidence": None,
                    "flags": None,
                    "is_lowest": False,
                    "candidates": [],
                    "missing_reason": "清单此项无比价组（所有供应商均未报价或未完成匹配）",
                }
                missing_cells += 1
            else:
                sid_items = [i for i in group.items if i.supplier_id == sid]
                cell = _build_review_cell(db, sid_items, sid)
                status = cell["cell_status"]
                if status == CELL_MISSING:
                    missing_cells += 1
                    cell["missing_reason"] = "该供应商未报价此品项"
                elif status == CELL_PENDING:
                    pending_cells += 1
                if status in (CELL_QUOTED, CELL_AGGREGATED) and cell["unit_price"]:
                    prices_this_row[sid] = cell["unit_price"]

            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED):
                quoted_count += 1
            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED, CELL_PENDING):
                covered_count += 1

            cells[str(sid)] = cell

        # Mark lowest among confirmed cells
        if prices_this_row:
            min_sid = min(prices_this_row, key=prices_this_row.__getitem__)
            cells[str(min_sid)]["is_lowest"] = True

        # Row status
        if n == 0 or quoted_count == n:
            row_status = "ok"
        elif quoted_count >= 2:
            row_status = "partial"
        elif covered_count >= 2:
            row_status = "pending"
        else:
            row_status = "missing"

        if quoted_count >= 2:
            quoted_ge_2 += 1
        if n > 0 and quoted_count == n:
            quoted_full += 1

        rows.append({
            "anchor_seq": seq_key,
            "anchor_name": anchor.name,
            "anchor_spec": anchor.spec or "",
            "anchor_pressure": anchor.pressure or "",
            "anchor_materials": anchor.material_text(),
            "anchor_brand": anchor.brand or "",
            "unit": anchor.unit or "",
            "quantity": anchor.qty,
            "row_status": row_status,
            "quoted_count": quoted_count,
            "covered_count": covered_count,
            "cells": cells,
        })

    # Compute matrix_distribution — convert to format expected by helper
    from apps.api.services.matrix_stats import build_matrix_distribution_from_rows
    fake_rows = [
        {
            "suppliers": [
                {
                    "supplier_id": sid,
                    "cell_status": row["cells"].get(str(sid), {}).get("cell_status", CELL_MISSING),
                    "price": row["cells"].get(str(sid), {}).get("unit_price"),
                }
                for sid in supplier_ids
            ]
        }
        for row in rows
    ]
    matrix_distribution = build_matrix_distribution_from_rows(fake_rows, supplier_ids)

    return {
        "anchors_total": len(anchors),
        "supplier_count": n,
        "pending_cells": pending_cells,
        "missing_cells": missing_cells,
        "quoted_ge_2_count": quoted_ge_2,
        "quoted_full_count": quoted_full,
        "suppliers": suppliers_info,
        "brand_requirement": session.brand_requirement or [],
        "matrix_distribution": matrix_distribution,
        "rows": rows,
    }
