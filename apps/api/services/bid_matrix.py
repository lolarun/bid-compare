"""Bid matrix comparison service — F6.1 横向对比矩阵."""

import string
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, Project
from apps.api.services.comparison import (
    compute_reasonable_low,
    compute_baseline,
    determine_alert,
    get_category_thresholds,
)


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

    # ── 3. 按物料构建矩阵行 ───────────────────────────────────────────────
    rows = []
    for mat in materials:
        mat_category = mat.category

        # 合理史低
        rl = compute_reasonable_low(db, mat_category, mat.sub_category or None)
        baseline = compute_baseline(db, mat_category, mat.sub_category or None)
        thresholds = get_category_thresholds(db, mat_category)

        reasonable_low_price = rl.get("reasonable_low")
        hist_avg = baseline.get("mean")
        sample_count = baseline.get("count", 0)

        # 历史均价信息
        historical_avg = None
        if hist_avg and sample_count > 0:
            # 粗略的时间范围（取最早/最晚报价日期）
            dates_q = db.query(Quote.quote_date).join(Material).filter(
                Material.category == mat_category,
                Quote.quote_date != "",
                Quote.quote_date.isnot(None),
            ).all()
            dates = sorted([r[0] for r in dates_q if r[0]])
            period = f"{dates[0]}~{dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "")
            projects_count = db.query(Quote.project_id).join(Material).filter(
                Material.category == mat_category,
                Quote.project_id.isnot(None),
            ).distinct().count()
            historical_avg = {
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

        # 各供应商报价
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

        # 标记最低价
        if prices_this_row:
            min_price_val = min(p for _, p, _ in prices_this_row)
            for cell in supplier_cells:
                if cell["price"] == min_price_val:
                    cell["is_lowest"] = True
                    break  # 只标记第一个

        # 最小偏差 & 推荐供应商
        deviations_with_sid = [(sid, dev) for sid, _, dev in prices_this_row if dev is not None]
        min_deviation = None
        recommended = None
        if deviations_with_sid:
            best = min(deviations_with_sid, key=lambda x: x[1])
            min_deviation = round(best[1], 4)
            recommended = letter_map.get(best[0])

        spec_text = mat.spec or ""
        rows.append({
            "material_id": mat.id,
            "material_name": mat.standard_name,
            "spec": spec_text,
            "historical_avg": historical_avg,
            "reasonable_low": reasonable_low_info,
            "suppliers": supplier_cells,
            "min_deviation": min_deviation,
            "recommended": recommended,
        })

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
    }
