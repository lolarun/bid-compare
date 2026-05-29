"""Bid matrix comparison service — F6.1 横向对比矩阵."""

import string
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, Project, BrandTier
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.services.comparison import (
    compute_reasonable_low,
    compute_baseline,
    determine_alert,
    get_category_thresholds,
)


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
    # Build quote lookup: supplier_id → Quote for this group
    quote_by_supplier: dict[int, Quote] = {}
    for item in ag.items:
        if item.action != "align":
            continue
        qt = db.get(Quote, item.quote_id)
        if qt and qt.unit_price and qt.unit_price > 0:
            quote_by_supplier[item.supplier_id] = qt

    if not quote_by_supplier:
        return None

    # Use the first aligned quote's material for baseline lookups
    first_quote = next(iter(quote_by_supplier.values()))
    mat = db.get(Material, first_quote.material_id)
    mat_category = mat.category if mat else ag.category

    historical_avg, reasonable_low_info = _compute_row_baselines(
        db, mat_category, mat.sub_category if mat else None, tier_filter,
    )
    thresholds = get_category_thresholds(db, mat_category)
    reasonable_low_price = reasonable_low_info["price"] if reasonable_low_info else None

    supplier_cells = []
    prices_this_row = []

    for sid in supplier_ids:
        qt = quote_by_supplier.get(sid)
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

    min_deviation, recommended = _finalize_row(supplier_cells, prices_this_row, letter_map)

    return {
        "material_id": first_quote.material_id,  # reference material
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
        alignment_groups = db.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
        ).all()
        for ag in alignment_groups:
            for item in ag.items:
                if item.action == "align":
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
        totals.append({
            "supplier_id": sid,
            "total": round(data["total"], 2),
            "avg_deviation": round(avg_dev, 4),
            "quoted_count": data["quoted"],
            "anomaly_count": data["anomalies"],
        })

    return {
        "project_id": project_id,
        "suppliers": supplier_labels,
        "rows": rows,
        "totals": totals,
        "brand_tier_filter": tier_filter,
    }
