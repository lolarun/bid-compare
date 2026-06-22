"""Bid matrix comparison service — F6.1 横向对比矩阵."""

import re as _re
import string
from dataclasses import dataclass
from sqlalchemy.orm import Session

from apps.api.core.enums import (
    CELL_QUOTED, CELL_AGGREGATED, CELL_PENDING, CELL_EXCLUDED, CELL_MISSING,
    REC_BLOCKED, REC_CONDITIONAL,
)
from apps.api.models import Material, Quote, Supplier, Project, BrandTier
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.extraction_job import ExtractionJob
from apps.api.services.comparison import (
    compute_reasonable_low,
    compute_baseline,
    determine_alert,
    get_category_thresholds,
    build_spec_price_index,
    spec_baseline_from_index,
)
from apps.api.services.canonical import extract_valve_canonical, normalize_valve_family
from apps.api.services.evaluation_policy import get_evaluation_policy


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
    # 税口径桥接（评标总价/同规格偏差用）；旧路径无则为 None/unknown
    price_basis: str | None = None           # incl_tax/dual_tax/excl_tax/unspecified/unknown
    unit_price_incl_tax: float | None = None  # 含税单价（评标总价唯一口径）
    unit: str = ""
    canonical: dict | None = None


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
        meta = bql.extraction_meta or {}
        return _ItemData(
            unit_price=bql.unit_price,
            quantity=bql.qty,
            total_price=bql.total_price,
            material_id=bql.material_id,
            source_quote_id=None,
            bid_quote_line_id=bql.id,
            standard_name=bql.standard_name,
            spec=bql.spec or "",
            price_basis=meta.get("price_basis"),
            # 桥接字段在 extraction_meta 里带 raw_ 前缀；effective_unit_price 已是该口径有效价
            unit_price_incl_tax=meta.get("raw_unit_price_incl_tax") or meta.get("effective_unit_price"),
            unit=bql.unit or "",
            canonical=bql.canonical or None,
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
            unit=(mat.unit or "") if mat else "",
        )

# CELL_* re-exported from core.enums for backward compatibility
# (tests/test_anchor_matrix.py imports these names from bid_matrix)
__all__ = [
    "CELL_QUOTED", "CELL_AGGREGATED", "CELL_PENDING", "CELL_EXCLUDED", "CELL_MISSING",
    "build_anchor_matrix", "build_anchor_review_matrix",
]


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


def _incl_unit_from(data: "_ItemData | None") -> float | None:
    """评标总价口径单价。税口径可作含税纳入时返回该价，否则 None（不得 ×1.13 换算）。

    - incl_tax/dual_tax 且有含税单价 → 该值；纯 incl_tax 用 unit_price。
    - unspecified（单一价格列，无含税/不含税之分）→ 按招标含税单价要求纳入该唯一价
      （price_basis.py 设计意图，用户 2026-06-22 确认；调用方须标 tax_basis_assumed）。
    - excl_tax/unknown → None。
    """
    if data is None:
        return None
    if data.price_basis in ("incl_tax", "dual_tax") and data.unit_price_incl_tax:
        return float(data.unit_price_incl_tax)
    if data.price_basis == "incl_tax" and data.unit_price:
        return float(data.unit_price)
    if data.price_basis == "unspecified":
        v = data.unit_price_incl_tax or data.unit_price
        return float(v) if v else None
    return None


_EVAL_QTY_TOL = 0.001


def _anchor_spec(anchor) -> tuple:
    """(family, dn, pn, unit) — 招标锚点的规格键（同规格基准/偏差以招标侧为准）。"""
    c = extract_valve_canonical(getattr(anchor, "name", "") or "", getattr(anchor, "spec", "") or "")
    fam = normalize_valve_family(c.get("valve_type"))
    return fam, c.get("dn"), c.get("pn"), (getattr(anchor, "unit", "") or "").strip()


def _canon_family(vt: str | None) -> str | None:
    """从原始阀型串归一化出族 — 与 _anchor_spec 同管线（extract_valve_canonical→normalize）。

    必须与锚点侧一致：normalize_valve_family('缓闭式止回阀')='缓闭式止回阀'（不降族），
    而 extract_valve_canonical 先抽出基础族'止回阀'。直接 normalize 存储 canonical 会
    与锚点族不对称，导致 quantity_source_conflict 被误判为 alignment_pending。
    """
    if not vt:
        return None
    return normalize_valve_family(extract_valve_canonical(vt, "").get("valve_type"))


def _pending_is_qty_only(cell: dict, fam, dn, a_unit) -> bool:
    """pending 单元格的冲突是否**仅数量来源**（DN/族/单位一致，价格口径可得）。

    仅当如此才允许纳入评标总价（标 quantity_source_conflict）；否则属对齐未决，不纳入。
    """
    c = cell.get("item_canonical") or {}
    c_dn = c.get("dn")
    c_fam = _canon_family(c.get("valve_type"))
    if dn and c_dn and dn != c_dn:
        return False
    if fam and c_fam and fam != c_fam:
        return False
    cu = (cell.get("unit") or "").strip()
    if a_unit and cu and a_unit != cu:
        return False
    return True


def _evaluate_cell(cell: dict, anchor_qty, fam, dn, pn, a_unit,
                   spec_index: dict, thresholds: dict) -> None:
    """就地填充单元格的同规格偏差 + 评标资格字段。

    评标金额恒 = 招标数量 × 含税单价（incl_unit）；供应商报价数量仅作校验。
    偏差相对同规格中位数（展示==计算）；无可靠同规格基准 → deviation=null、不计异常。
    """
    cell["tender_qty"] = anchor_qty
    cell["eval_amount"] = None
    cell["evaluable"] = False
    cell["baseline"] = None
    cell["tax_basis_assumed"] = False
    status = cell["cell_status"]
    basis = cell.get("price_basis")
    incl_unit = cell.get("incl_unit")

    if status in (CELL_MISSING, CELL_EXCLUDED) or cell.get("price") is None:
        cell["eval_status"] = "missing" if status == CELL_MISSING else status
        cell["deviation_pct"] = None
        cell["alert_level"] = "normal"
        return

    # ── 同规格偏差：按本格税口径取对应桶；展示值与计算值同源（中位数）──
    # unspecified（单一价格列）按招标含税要求与含税桶比较。
    bl = None
    cmp_price = None
    if incl_unit is not None and basis in ("incl_tax", "dual_tax", "unspecified"):
        bl = spec_baseline_from_index(spec_index, fam, dn, pn, a_unit, "incl_tax")
        cmp_price = incl_unit
    elif basis == "excl_tax" and cell.get("price"):
        bl = spec_baseline_from_index(spec_index, fam, dn, pn, a_unit, "excl_tax")
        cmp_price = cell["price"]
    if bl and cmp_price:
        dev = round((cmp_price - bl["median"]) / bl["median"], 4)
        cell["deviation_pct"] = dev
        cell["alert_level"] = determine_alert(dev, thresholds)
        cell["baseline"] = bl
    else:
        cell["deviation_pct"] = None
        cell["alert_level"] = "normal"   # 无可靠同规格基准 → 不计异常

    # ── 评标资格：必须有可纳入的含税口径单价 ──
    if incl_unit is None:
        cell["eval_status"] = "basis_unconfirmed"   # 税口径未确认（excl_tax/unknown）→ 未决（不静默排除）
        return
    # 单一价格列按招标含税要求纳入，但标记假定（非确认），供风险提示与人工核实。
    cell["tax_basis_assumed"] = (basis == "unspecified")
    eval_amount = round(float(anchor_qty) * incl_unit, 2) if anchor_qty else None
    sq = cell.get("supplier_qty")
    qty_conflict = (
        sq is not None and anchor_qty is not None
        and abs(float(sq) - float(anchor_qty)) > _EVAL_QTY_TOL
    )
    if status == CELL_PENDING:
        if _pending_is_qty_only(cell, fam, dn, a_unit):
            cell["eval_status"] = "quantity_source_conflict"
            cell["evaluable"] = True
            cell["eval_amount"] = eval_amount
        else:
            cell["eval_status"] = "alignment_pending"   # 非数量原因未决 → 不纳入
        return
    # align / aggregated
    cell["evaluable"] = True
    cell["eval_amount"] = eval_amount
    cell["eval_status"] = "quantity_source_conflict" if qty_conflict else "ok"


def _build_cell_for_supplier(
    db: Session,
    items: list[BidAlignmentItem],
    sid: int,
) -> dict:
    """Build a SupplierCell dict for one (group, supplier) combination.

    Priority: align/aggregated > pending > excluded > missing.
    价格基准/偏差/评标资格在行循环中按同规格基准计算，这里只取价格与税口径原料。
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
        # 税口径/评标原料（行循环用）
        "price_basis": None,
        "incl_unit": None,
        "unit": "",
        "supplier_qty": None,
        "item_canonical": None,
    }

    def _fill(cell: dict, item: BidAlignmentItem, data: "_ItemData | None") -> None:
        if not data:
            return
        if item.agg_total is not None and item.agg_qty:
            price = round(item.agg_total / item.agg_qty, 4)
            total = round(item.agg_total, 2)
        else:
            price = data.unit_price
            total = round(price * (data.quantity or 1), 2) if price else None
        cell["price"] = price
        cell["total"] = total
        cell["source_quote_id"] = data.source_quote_id
        cell["bid_quote_line_id"] = data.bid_quote_line_id
        cell["price_basis"] = data.price_basis
        cell["incl_unit"] = _incl_unit_from(data)
        cell["unit"] = data.unit
        cell["supplier_qty"] = data.quantity
        cell["item_canonical"] = data.canonical

    if align_items:
        def _effective_price(i: BidAlignmentItem) -> float:
            if i.agg_total is not None and i.agg_qty:
                return i.agg_total / i.agg_qty
            data = _get_item_data(db, i)
            return (data.unit_price or float("inf")) if data else float("inf")

        best = min(align_items, key=_effective_price)
        _fill(base, best, _get_item_data(db, best))
        base["cell_status"] = CELL_AGGREGATED if (best.agg_total is not None) else CELL_QUOTED
        base["flags"] = _parse_flags_from_note(best.spec_note) or None
        base["evidence"] = best.name_note or None
        if pending_items:
            base["pending_note"] = f"另有 {len(pending_items)} 条待确认"
        return base

    if pending_items:
        best = max(pending_items, key=lambda i: _parse_cosine_from_note(i.spec_note) or 0)
        _fill(base, best, _get_item_data(db, best))
        base["cell_status"] = CELL_PENDING
        base["item_id"] = best.id
        base["confidence"] = _parse_cosine_from_note(best.spec_note)
        base["flags"] = _parse_flags_from_note(best.spec_note) or None
        base["evidence"] = best.name_note or None
        return base

    if excluded_items:
        base["cell_status"] = CELL_EXCLUDED
        return base

    return base  # missing


def _compute_recommendation(
    rows: list[dict],
    col_ids: list[int],
    supplier_labels: list[dict],
    total_anchors: int,
    checksum_by_col: dict[int, str],
    policy,
) -> dict:
    """招标文件驱动的确定性推荐（不自动定标、不拆单、不造权重）。

    产出：评标总价排名(价格优选候选人) + 共同可比金额 + 未决行金额 + 非价格因素证据缺口 +
    三态 recommendation_level。LLM 仅据此解释，不得改选。
    """
    label_by = {sl["id"]: sl for sl in supplier_labels}
    per = {cid: {"evaluated_total": 0.0, "confirmed_lines": 0, "qty_conflict_lines": 0,
                 "undecided_lines": 0, "undecided_amount": 0.0, "missing_lines": 0,
                 "anomaly_count": 0, "basis_unconfirmed_lines": 0,
                 "tax_assumed_lines": 0} for cid in col_ids}
    evaluable_by_line: list[set] = []
    for row in rows:
        eset: set = set()
        cell_by = {c["supplier_id"]: c for c in row["suppliers"]}
        for cid in col_ids:
            c = cell_by.get(cid, {})
            st = c.get("eval_status")
            if c.get("evaluable"):
                per[cid]["confirmed_lines"] += 1
                per[cid]["evaluated_total"] += c.get("eval_amount") or 0.0
                eset.add(cid)
                if st == "quantity_source_conflict":
                    per[cid]["qty_conflict_lines"] += 1
                if c.get("tax_basis_assumed"):
                    per[cid]["tax_assumed_lines"] += 1
            elif st in ("basis_unconfirmed", "alignment_pending"):
                per[cid]["undecided_lines"] += 1
                if st == "basis_unconfirmed":
                    per[cid]["basis_unconfirmed_lines"] += 1
                ref_unit = c.get("incl_unit") or c.get("price")
                if c.get("tender_qty") and ref_unit:
                    per[cid]["undecided_amount"] += round(float(c["tender_qty"]) * float(ref_unit), 2)
            else:
                per[cid]["missing_lines"] += 1
            if c.get("alert_level") == "red":
                per[cid]["anomaly_count"] += 1
        evaluable_by_line.append(eset)

    supplier_eval = []
    for cid in col_ids:
        p = per[cid]
        cs = checksum_by_col.get(cid, "unknown")
        full = (total_anchors > 0 and p["confirmed_lines"] == total_anchors)
        eligible = full and cs != "fail"
        supplier_eval.append({
            "supplier_id": cid,
            "name": label_by.get(cid, {}).get("name"),
            "letter": label_by.get(cid, {}).get("letter"),
            "evaluated_total": round(p["evaluated_total"], 2),
            "confirmed_lines": p["confirmed_lines"],
            "total_anchors": total_anchors,
            "qty_conflict_lines": p["qty_conflict_lines"],
            "undecided_lines": p["undecided_lines"],
            "undecided_amount": round(p["undecided_amount"], 2),
            "missing_lines": p["missing_lines"],
            "anomaly_count": p["anomaly_count"],
            "tax_assumed_lines": p["tax_assumed_lines"],
            # 税口径"确认"须同时无未决口径与无假定口径；单一价格列属假定，非确认。
            "basis_confirmed": (p["basis_unconfirmed_lines"] == 0
                                and p["tax_assumed_lines"] == 0
                                and p["confirmed_lines"] > 0),
            "checksum_status": cs,
            "full_coverage": full,
            "eligible_for_ranking": eligible,
        })

    ranked = sorted(
        [s for s in supplier_eval if s["eligible_for_ranking"]],
        key=lambda s: s["evaluated_total"],
    )
    ranked_ids = [s["supplier_id"] for s in ranked]
    # 共同可比金额：所有入排名供应商**均可评标**的行
    common_lines = 0
    common_sub = {cid: 0.0 for cid in ranked_ids}
    for ridx, row in enumerate(rows):
        if ranked_ids and all(cid in evaluable_by_line[ridx] for cid in ranked_ids):
            common_lines += 1
            cell_by = {c["supplier_id"]: c for c in row["suppliers"]}
            for cid in ranked_ids:
                common_sub[cid] += cell_by[cid].get("eval_amount") or 0.0
    price_preferred = ranked[0] if ranked else None

    risks: list[str] = []
    for s in supplier_eval:
        nm = s["name"]
        if s["checksum_status"] == "fail":
            risks.append(f"{nm} 核验金额不符（checksum fail）")
        elif s["checksum_status"] in ("unknown", None):
            risks.append(f"{nm} 无声明总价核验（风险提示，未阻断）")
        if s["qty_conflict_lines"]:
            risks.append(
                f"{nm} {s['qty_conflict_lines']} 行报价数量≠招标数量，已按招标数量计入评标"
                "（标记 quantity_source_conflict，建议核实）"
            )
        if s.get("tax_assumed_lines"):
            risks.append(
                f"{nm} {s['tax_assumed_lines']} 行为单一价格列（无含税/不含税标注），"
                "已按招标含税单价要求纳入评标（税口径假定含税，建议核实）"
            )
        if s["undecided_lines"]:
            risks.append(
                f"{nm} {s['undecided_lines']} 行税口径/对齐未确认，未计入完整评标总价"
                f"（未决金额≈¥{s['undecided_amount']:,.0f}）"
            )
        if s["anomaly_count"]:
            risks.append(f"{nm} {s['anomaly_count']} 行同规格偏差异常，建议核实异常低价")
        if s["missing_lines"]:
            risks.append(f"{nm} {s['missing_lines']} 行缺报")

    reasons: list[str] = []
    if not ranked:
        level = REC_BLOCKED
        reasons.append("无任一供应商可形成完整含税评标总价（税口径/对齐未确认或缺报）→ 无价格优选候选人")
    else:
        level = REC_CONDITIONAL
        reasons.append("已得确定性『评标总价排名』与『价格优选候选人』")
        if policy.method == "unknown":
            reasons.append("评标法尚未确认 → 价格排名仅供参考，定标需人工确认招标文件评标法后方可进行")
        else:
            reasons.append("招标文件为合理低价评标价法且未给评分权重 → 综合评审需招标领导小组确认，非自动中标")

    # 非价格八项因素证据：系统无结构化证据 → 一律 missing（待评标小组）
    non_price = [{"factor": f, "evidence_status": "missing"} for f in policy.factors if f != "价格"]

    return {
        "recommendation_level": level,
        "recommendation_reasons": reasons,
        "risks": risks,
        "evaluation_policy": policy.to_dict(),
        "award_mode": policy.award_mode,
        "committee_required": policy.final_decision_requires_committee,
        "price_ranking": ranked,
        "price_preferred_candidate": price_preferred,
        "supplier_evaluation": supplier_eval,
        "common_comparable": {
            "supplier_ids": ranked_ids,
            "line_count": common_lines,
            "subtotals": {str(k): round(v, 2) for k, v in common_sub.items()},
        },
        "non_price_factors": non_price,
        "comprehensive_recommendation_status": "pending_committee",
    }


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

    # ── 同规格基准索引（一次扫描品类历史）+ 评标政策 ──────────────────────────
    spec_index = build_spec_price_index(db, category)
    policy = get_evaluation_policy(project_id)
    thresholds = get_category_thresholds(db, category)

    # ── Build one row per anchor ─────────────────────────────────────────────
    rows = []
    supplier_totals: dict[int, dict] = {
        col_id: {"total": 0.0, "devs": [], "quoted": 0, "anomalies": 0}
        for col_id in col_ids
    }

    for anchor in anchors:
        seq_key = str(anchor.seq)
        group = seq_to_group.get(seq_key)
        fam, dn, pn, a_unit = _anchor_spec(anchor)
        anchor_qty = getattr(anchor, "qty", None)

        ref_material_id: int | None = None
        if group:
            for it in group.items:
                if it.action == "align":
                    _rd = _get_item_data(db, it)
                    if _rd:
                        ref_material_id = _rd.material_id
                        break

        supplier_cells: list[dict] = []
        prices_this_row: list[tuple] = []  # (col_id, 含税单价, deviation) — 评标口径
        row_baseline: dict | None = None

        for col_id in col_ids:
            actual_sid = sub_actual_sids.get(col_id, col_id) if use_submission_mode else col_id
            if group is None:
                cell = {
                    "supplier_id": col_id, "price": None, "total": None,
                    "deviation_pct": None, "alert_level": "normal", "is_lowest": False,
                    "cell_status": CELL_MISSING, "item_id": None, "confidence": None,
                    "source_quote_id": None, "bid_quote_line_id": None, "pending_note": None,
                    "price_basis": None, "incl_unit": None, "unit": "",
                    "supplier_qty": None, "item_canonical": None,
                }
            else:
                col_items = _items_for_col(group.items, col_id, actual_sid)
                cell = _build_cell_for_supplier(db, col_items, col_id)

            # 同规格偏差 + 评标资格（评标金额 = 招标数量 × 含税单价）
            _evaluate_cell(cell, anchor_qty, fam, dn, pn, a_unit, spec_index, thresholds)
            supplier_cells.append(cell)
            if cell.get("baseline") and row_baseline is None:
                row_baseline = cell["baseline"]

            if cell.get("price") is not None and cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED, CELL_PENDING):
                supplier_totals[col_id]["quoted"] += 1
            if cell.get("evaluable"):
                supplier_totals[col_id]["total"] += cell.get("eval_amount") or 0.0
                if cell.get("incl_unit") is not None:
                    prices_this_row.append((col_id, cell["incl_unit"], cell.get("deviation_pct")))
            if cell.get("deviation_pct") is not None:
                supplier_totals[col_id]["devs"].append(cell["deviation_pct"])
            if cell.get("alert_level") == "red":
                supplier_totals[col_id]["anomalies"] += 1

        # 最低含税评标单价标记（仅展示）；行级 recommended 仅在允许拆单时给
        if prices_this_row:
            min_p = min(p for _, p, _ in prices_this_row)
            for cid, p, _ in prices_this_row:
                if p == min_p:
                    for c in supplier_cells:
                        if c["supplier_id"] == cid:
                            c["is_lowest"] = True
                    break
        min_deviation = min((d for _, _, d in prices_this_row if d is not None), default=None)
        recommended = None  # single_supplier：不做分项授标推荐
        if policy.allows_split_award and prices_this_row:
            recommended = letter_map.get(min(prices_this_row, key=lambda x: x[1])[0])

        # 展示基准 == 偏差计算基准（同规格中位数）
        historical_avg = (
            {"price": row_baseline["median"], "spec_key": row_baseline["spec_key"],
             "count": row_baseline["count"], "basis": row_baseline["basis"]}
            if row_baseline else None
        )

        rows.append({
            "material_id": ref_material_id,
            "material_name": anchor.name,
            "spec": getattr(anchor, "spec", "") or "",
            "materials": anchor.material_text() if hasattr(anchor, "material_text") else "",
            "brand": getattr(anchor, "brand", "") or "",
            "anchor_seq": str(anchor.seq),
            "unit": a_unit,
            "quantity": anchor_qty,
            "historical_avg": historical_avg,
            "spec_baseline": historical_avg,
            "reasonable_low": None,   # 弃用全品类地板价（假异常根因）
            "suppliers": supplier_cells,
            "min_deviation": min_deviation,
            "recommended": recommended,
        })

    # ── Totals（含税评标口径）+ checksum（仅 fail 阻断）──────────────────────────
    totals = []
    checksum_by_col: dict[int, str] = {}
    for col_id in col_ids:
        data = supplier_totals[col_id]
        avg_dev: float | None = (
            round(sum(data["devs"]) / len(data["devs"]), 4) if data["devs"] else None
        )
        if use_submission_mode:
            sub = db.get(BidSubmission, col_id)
            cs = _get_submission_checksum(db, sub) if sub else {}
        else:
            cs = _get_supplier_checksum(db, col_id, project_id)
        checksum_status = cs.get("status", "unknown")
        checksum_by_col[col_id] = checksum_status
        totals.append({
            "supplier_id": col_id,
            "total": round(data["total"], 2),   # 含税评标总价（招标数量×含税单价，确认行）
            "avg_deviation": avg_dev,
            "quoted_count": data["quoted"],
            "anomaly_count": data["anomalies"],
            "declared_total": cs.get("declared"),
            "checksum_delta_pct": cs.get("delta_pct"),
            "checksum_status": checksum_status,
        })

    total_anchors = len(anchors)
    rec = _compute_recommendation(rows, col_ids, supplier_labels, total_anchors, checksum_by_col, policy)
    eval_by = {s["supplier_id"]: s for s in rec["supplier_evaluation"]}
    for t in totals:
        se = eval_by.get(t["supplier_id"], {})
        t["evaluated_total"] = se.get("evaluated_total")
        t["confirmed_lines"] = se.get("confirmed_lines")
        t["qty_conflict_lines"] = se.get("qty_conflict_lines")
        t["undecided_lines"] = se.get("undecided_lines")
        t["undecided_amount"] = se.get("undecided_amount")
        t["tax_assumed_lines"] = se.get("tax_assumed_lines")
        t["basis_confirmed"] = se.get("basis_confirmed")
        t["eligible_for_ranking"] = se.get("eligible_for_ranking")

    from apps.api.services.matrix_stats import build_matrix_distribution_from_rows
    matrix_distribution = build_matrix_distribution_from_rows(rows, col_ids)

    blocked = rec["recommendation_level"] == REC_BLOCKED
    return {
        "project_id": project_id,
        "suppliers": supplier_labels,
        "rows": rows,
        "totals": totals,
        "brand_tier_filter": tier_filter,
        "anchor_matrix": True,
        "matrix_distribution": matrix_distribution,
        # 三态门禁 + 招标文件驱动的评标
        "recommendation_level": rec["recommendation_level"],
        "recommendation_reasons": rec["recommendation_reasons"],
        "risks": rec["risks"],
        "evaluation_policy": rec["evaluation_policy"],
        "award_mode": rec["award_mode"],
        "committee_required": rec["committee_required"],
        "price_ranking": rec["price_ranking"],
        "price_preferred_candidate": rec["price_preferred_candidate"],
        "supplier_evaluation": rec["supplier_evaluation"],
        "common_comparable": rec["common_comparable"],
        "non_price_factors": rec["non_price_factors"],
        "comprehensive_recommendation_status": rec["comprehensive_recommendation_status"],
        # 兼容旧前端（过渡）：仅 blocked 置 true，卡片/AI 不再因 conditional 隐藏
        "recommendation_blocked": blocked,
        "recommendation_blocked_reasons": (rec["recommendation_reasons"] if blocked else []),
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
    submission_ids: list[int] | None = None,
    supplier_ids: list[int] | None = None,  # legacy path: Supplier.id; new BID path uses submission_ids
) -> dict:
    """Anchor-first review matrix for the pre-review UI.

    Two modes:
    - New BID path (submission_ids provided): cells keyed by str(submission_id),
      looks up BidSubmission for display name, filters alignment items by i.submission_id.
    - Legacy path (supplier_ids provided): cells keyed by str(supplier_id),
      looks up Supplier for display name, filters alignment items by i.supplier_id.

    submission_ids takes precedence. At least one of the two must be non-empty.
    """
    from apps.api.models.bid_submission import BidSubmission
    from apps.api.services.tender_list import rebuild_anchors
    from apps.api.services.tender_session_service import get_current_confirmed_session

    session = get_current_confirmed_session(db, project_id, category)
    if not session:
        raise ValueError(f"No current TenderListSession for project {project_id} / {category}")

    anchors = rebuild_anchors(session)

    use_submission_path = bool(submission_ids)

    if use_submission_path:
        ids = sorted(set(submission_ids))  # type: ignore[arg-type]
    elif supplier_ids:
        ids = sorted(set(supplier_ids))
    else:
        raise ValueError(
            "submission_ids 不可为空 — 比价流程禁止扫历史全量供应商。"
            "请先完成供应商报价上传并「开始匹配」后再查看复核矩阵。"
        )

    # ── Build suppliers_info ──────────────────────────────────────────────────
    # New path: BidSubmission lookup; Legacy path: Supplier lookup + supplier_brand_map
    supplier_brand: dict[int, str] = {}
    if not use_submission_path:
        for sb in (session.supplier_brand_map or []):
            if isinstance(sb, dict) and sb.get("supplier_id") is not None and sb.get("brand"):
                supplier_brand[int(sb["supplier_id"])] = str(sb["brand"])

    suppliers_info = []
    for col_id in ids:
        if use_submission_path:
            sub = db.get(BidSubmission, col_id)
            if not sub:
                continue
            cs = _get_submission_checksum(db, sub)
            suppliers_info.append({
                "submission_id": col_id,
                "supplier_id": sub.supplier_id,       # nullable soft-ref
                "supplier_name": sub.supplier_raw_name,
                "supplier_raw_name": sub.supplier_raw_name,
                "brand": "",
                "checksum_status": cs.get("status"),
                "declared_total": cs.get("declared"),
                "checksum_delta_pct": cs.get("delta_pct"),
            })
        else:
            sup = db.get(Supplier, col_id)
            if not sup:
                continue
            cs = _get_supplier_checksum(db, col_id, project_id)
            suppliers_info.append({
                "submission_id": None,
                "supplier_id": col_id,
                "supplier_name": sup.name,
                "supplier_raw_name": sup.name,
                "brand": supplier_brand.get(col_id, ""),
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

    # Build rows — cells keyed by str(col_id) [submission_id or supplier_id depending on mode]
    rows = []
    pending_cells = 0
    missing_cells = 0
    quoted_ge_2 = 0
    quoted_full = 0
    n = len(ids)

    for anchor in anchors:
        seq_key = str(anchor.seq)
        group = seq_to_group.get(seq_key)

        cells: dict[str, dict] = {}
        quoted_count = 0
        covered_count = 0
        prices_this_row: dict[int, float] = {}

        for col_id in ids:
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
                if use_submission_path:
                    col_items = [i for i in group.items if i.submission_id == col_id]
                else:
                    col_items = [i for i in group.items if i.supplier_id == col_id]
                cell = _build_review_cell(db, col_items, col_id)
                status = cell["cell_status"]
                if status == CELL_MISSING:
                    missing_cells += 1
                    cell["missing_reason"] = "该供应商未报价此品项"
                elif status == CELL_PENDING:
                    pending_cells += 1
                if status in (CELL_QUOTED, CELL_AGGREGATED) and cell["unit_price"]:
                    prices_this_row[col_id] = cell["unit_price"]

            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED):
                quoted_count += 1
            if cell["cell_status"] in (CELL_QUOTED, CELL_AGGREGATED, CELL_PENDING):
                covered_count += 1

            cells[str(col_id)] = cell

        # Mark lowest among confirmed cells
        if prices_this_row:
            min_col = min(prices_this_row, key=prices_this_row.__getitem__)
            cells[str(min_col)]["is_lowest"] = True

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
                    "supplier_id": col_id,
                    "cell_status": row["cells"].get(str(col_id), {}).get("cell_status", CELL_MISSING),
                    "price": row["cells"].get(str(col_id), {}).get("unit_price"),
                }
                for col_id in ids
            ]
        }
        for row in rows
    ]
    matrix_distribution = build_matrix_distribution_from_rows(fake_rows, ids)

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
