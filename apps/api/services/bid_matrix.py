"""Bid matrix comparison service — F6.1 横向对比矩阵."""

import re as _re
import string
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
    # Build lookup: supplier_id → (item, Quote) — item carries agg_total/agg_qty
    item_by_supplier: dict[int, tuple] = {}
    for item in ag.items:
        if item.action != "align":
            continue
        qt = db.get(Quote, item.quote_id)
        if qt and qt.unit_price and qt.unit_price > 0:
            sid = item.supplier_id
            existing = item_by_supplier.get(sid)
            if existing is None or qt.unit_price < existing[1].unit_price:
                item_by_supplier[sid] = (item, qt)

    if not item_by_supplier:
        return None

    # Use the first aligned quote's material for baseline lookups
    first_qt = next(iter(item_by_supplier.values()))[1]
    mat = db.get(Material, first_qt.material_id)
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
        "pending_note": None,
        "flags": None,
        "evidence": None,
    }

    def _price_from_item(item: BidAlignmentItem) -> tuple[float | None, float | None]:
        """Return (unit_price, total) — uses agg when available."""
        qt = db.get(Quote, item.quote_id)
        if not qt:
            return None, None
        if item.agg_total is not None and item.agg_qty:
            price = round(item.agg_total / item.agg_qty, 4)
            total = round(item.agg_total, 2)
        else:
            price = qt.unit_price
            total = round(price * (qt.quantity or 1), 2) if price else None
        return price, total

    def _fill_price(cell: dict, price: float | None, total: float | None,
                    source_qid: int | None) -> None:
        cell["price"] = price
        cell["total"] = total
        cell["source_quote_id"] = source_qid
        if price and reasonable_low_price:
            dev = round((price - reasonable_low_price) / reasonable_low_price, 4)
            cell["deviation_pct"] = dev
            cell["alert_level"] = determine_alert(dev, thresholds) if dev is not None else "normal"

    if align_items:
        # Pick best align item: prefer aggregated; among multiple, take lowest effective price
        def _effective_price(i: BidAlignmentItem) -> float:
            if i.agg_total is not None and i.agg_qty:
                return i.agg_total / i.agg_qty
            qt = db.get(Quote, i.quote_id)
            return (qt.unit_price or float("inf")) if qt else float("inf")

        best = min(align_items, key=_effective_price)
        price, total = _price_from_item(best)
        qt = db.get(Quote, best.quote_id)
        _fill_price(base, price, total, qt.id if qt else None)
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
        price, total = _price_from_item(best)
        qt = db.get(Quote, best.quote_id)
        _fill_price(base, price, total, qt.id if qt else None)
        base["cell_status"] = CELL_PENDING
        base["item_id"] = best.id
        base["confidence"] = _parse_cosine_from_note(best.spec_note)
        base["source_quote_id"] = qt.id if qt else None
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
) -> dict:
    """Build the bid matrix anchored to ALL tender list items (v2.5).

    Every anchor becomes exactly one matrix row regardless of whether suppliers quoted it.
    Cell statuses: quoted / aggregated / pending / excluded / missing.
    Pending cells show reference price (method A) but are excluded from:
      - is_lowest calculation
      - supplier total price
      - avg_deviation
      - recommended supplier
    """
    # ── Supplier labels ──────────────────────────────────────────────────────
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

    # ── Load all groups for this project/category ────────────────────────────
    q = db.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == project_id,
        BidAlignmentGroup.category == category,
        BidAlignmentGroup.status == "confirmed",
    )
    if allowed_group_ids is not None:
        q = q.filter(BidAlignmentGroup.id.in_(allowed_group_ids))
    all_groups: list[BidAlignmentGroup] = q.all()

    # Build lookup: anchor_seq → group (prefer session-matched, fallback to any)
    seq_to_group: dict[str, BidAlignmentGroup] = {}
    for g in all_groups:
        if g.anchor_seq is None:
            continue
        seq = str(g.anchor_seq)
        if seq not in seq_to_group:
            seq_to_group[seq] = g
        elif (tender_list_session_id is not None
              and g.tender_list_session_id == tender_list_session_id):
            # Prefer the group from the current session
            seq_to_group[seq] = g

    tier_filter = _detect_brand_tier_filter(db, supplier_ids, category, project_id)

    # ── Build one row per anchor ─────────────────────────────────────────────
    rows = []
    supplier_totals: dict[int, dict] = {
        sid: {"total": 0.0, "devs": [], "quoted": 0, "anomalies": 0}
        for sid in supplier_ids
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
            ref_mat = db.get(Material, first_align.quote_id) if first_align else None
            if first_align:
                ref_qt = db.get(Quote, first_align.quote_id)
                ref_mat = db.get(Material, ref_qt.material_id) if ref_qt else None
            mat_category = ref_mat.category if ref_mat else category
            sub_cat = ref_mat.sub_category if ref_mat else None
        else:
            mat_category = category
            sub_cat = None

        historical_avg, reasonable_low_info = _compute_row_baselines(
            db, mat_category, sub_cat, tier_filter
        )
        thresholds = get_category_thresholds(db, mat_category)
        reasonable_low_price = reasonable_low_info["price"] if reasonable_low_info else None

        # ── Build cells for each supplier ─────────────────────────────────
        supplier_cells: list[dict] = []
        prices_this_row: list[tuple] = []  # only quoted/aggregated

        for sid in supplier_ids:
            if group is None:
                # No match at all — missing for all suppliers
                cell = {
                    "supplier_id": sid,
                    "price": None,
                    "total": None,
                    "deviation_pct": None,
                    "alert_level": "normal",
                    "is_lowest": False,
                    "cell_status": CELL_MISSING,
                    "item_id": None,
                    "confidence": None,
                    "source_quote_id": None,
                    "pending_note": None,
                }
            else:
                # Filter items belonging to this supplier
                sid_items = [i for i in group.items if i.supplier_id == sid]
                cell = _build_cell_for_supplier(
                    db, sid_items, sid,
                    reasonable_low_price, thresholds, letter_map,
                )

            supplier_cells.append(cell)

            # Only confirmed cells participate in lowest/totals
            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED) and cell["price"] is not None:
                prices_this_row.append((sid, cell["price"], cell.get("deviation_pct")))
                supplier_totals[sid]["quoted"] += 1
                if cell["total"] is not None:
                    supplier_totals[sid]["total"] += cell["total"]
                if cell["deviation_pct"] is not None:
                    supplier_totals[sid]["devs"].append(cell["deviation_pct"])
            if cell.get("alert_level") == "red":
                supplier_totals[sid]["anomalies"] += 1

        # Mark lowest price (among quoted/aggregated only)
        min_deviation, recommended = _finalize_row(supplier_cells, prices_this_row, letter_map)

        # Use first confirmed quote's material_id as row reference
        ref_material_id: int | None = None
        if group:
            for it in group.items:
                if it.action == "align":
                    qt = db.get(Quote, it.quote_id)
                    if qt:
                        ref_material_id = qt.material_id
                        break

        rows.append({
            "material_id": ref_material_id,
            "material_name": anchor.name,
            "spec": getattr(anchor, "spec", "") or "",
            "anchor_seq": str(anchor.seq),
            "historical_avg": historical_avg,
            "reasonable_low": reasonable_low_info,
            "suppliers": supplier_cells,
            "min_deviation": min_deviation,
            "recommended": recommended,
        })

    # ── Totals (quoted-only cells) ────────────────────────────────────────────
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

    from apps.api.services.matrix_stats import build_matrix_distribution_from_rows
    matrix_distribution = build_matrix_distribution_from_rows(rows, supplier_ids)

    return {
        "project_id": project_id,
        "suppliers": supplier_labels,
        "rows": rows,
        "totals": totals,
        "brand_tier_filter": tier_filter,
        "anchor_matrix": True,  # flag for frontend to know it's anchor-driven
        "matrix_distribution": matrix_distribution,
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
        "unit_price": None,
        "total_price": None,
        "confidence": None,
        "evidence": None,
        "flags": None,
        "is_lowest": False,
        "candidates": [],
    }

    def _get_prices(item: BidAlignmentItem) -> tuple:
        qt = db.get(Quote, item.quote_id)
        if not qt:
            return None, None, None
        if item.agg_total is not None and item.agg_qty:
            price = round(item.agg_total / item.agg_qty, 4)
            total = round(item.agg_total, 2)
        else:
            price = qt.unit_price
            total = round(price * (qt.quantity or 1), 2) if price else None
        return price, total, qt.id

    def _build_candidates(plist: list) -> list:
        out = []
        for item in sorted(plist, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0, reverse=True)[:5]:
            qt = db.get(Quote, item.quote_id)
            if not qt:
                continue
            mat = db.get(Material, qt.material_id)
            out.append({
                "item_id": item.id,
                "quote_id": item.quote_id,
                "material_name": mat.standard_name if mat else "",
                "spec": (mat.spec or "") if mat else "",
                "unit_price": qt.unit_price,
                "confidence": _parse_cosine_from_note(item.spec_note),
                "flags": _parse_flags_from_note(item.spec_note) or None,
            })
        return out

    if align_items:
        def _eff(i: BidAlignmentItem) -> float:
            if i.agg_total is not None and i.agg_qty:
                return i.agg_total / i.agg_qty
            qt = db.get(Quote, i.quote_id)
            return (qt.unit_price or float("inf")) if qt else float("inf")

        best = min(align_items, key=_eff)
        price, total, quote_id = _get_prices(best)
        base.update({
            "cell_status": CELL_AGGREGATED if best.agg_total is not None else CELL_QUOTED,
            "item_id": best.id,
            "quote_id": quote_id,
            "unit_price": price,
            "total_price": total,
            "evidence": best.name_note or None,
            "flags": _parse_flags_from_note(best.spec_note) or None,
            "candidates": _build_candidates(pending_items),
        })
        return base

    if pending_items:
        best = max(pending_items, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0)
        price, total, quote_id = _get_prices(best)
        base.update({
            "cell_status": CELL_PENDING,
            "item_id": best.id,
            "quote_id": quote_id,
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


def build_anchor_review_matrix(db: Session, project_id: int, category: str) -> dict:
    """Anchor-first review matrix for the pre-review UI.

    Returns one row per TenderAnchor with cells dict keyed by str(supplier_id).
    Includes candidates list for pending cells. Does not compute deviations or
    alert levels (those are for the final bid matrix).
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

    # Discover suppliers from quotes in this project+category
    raw = (
        db.query(Quote.supplier_id)
        .join(Material, Quote.material_id == Material.id)
        .filter(
            Quote.project_id == project_id,
            Material.category == category,
            Quote.supplier_id.isnot(None),
        )
        .distinct()
        .all()
    )
    supplier_ids = sorted({sid for (sid,) in raw})

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
            "checksum_status": cs.get("status"),
            "declared_total": cs.get("declared"),
            "checksum_delta_pct": cs.get("delta_pct"),
        })

    # Load confirmed groups → seq → group map
    all_groups = (
        db.query(BidAlignmentGroup)
        .filter(
            BidAlignmentGroup.project_id == project_id,
            BidAlignmentGroup.category == category,
            BidAlignmentGroup.status == "confirmed",
        )
        .all()
    )
    seq_to_group: dict[str, BidAlignmentGroup] = {}
    for g in all_groups:
        if g.anchor_seq is None:
            continue
        seq = str(g.anchor_seq)
        if seq not in seq_to_group or g.tender_list_session_id == session.id:
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
                }
                missing_cells += 1
            else:
                sid_items = [i for i in group.items if i.supplier_id == sid]
                cell = _build_review_cell(db, sid_items, sid)
                status = cell["cell_status"]
                if status == CELL_MISSING:
                    missing_cells += 1
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
        "matrix_distribution": matrix_distribution,
        "rows": rows,
    }
