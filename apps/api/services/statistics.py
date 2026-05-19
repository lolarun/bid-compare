"""Dashboard summary, category stats, and baseline refresh service."""

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Supplier, Project
from apps.api.core.config import PROFESSION_MAP
from apps.api.services.comparison import compute_baseline


def get_dashboard_summary(db: Session) -> dict:
    total_materials = db.query(func.count(Material.id)).scalar() or 0
    total_suppliers = db.query(func.count(Supplier.id)).scalar() or 0
    total_projects = db.query(func.count(Project.id)).scalar() or 0
    total_quotes = db.query(func.count(Quote.id)).scalar() or 0

    categories = db.query(Material.category).distinct().all()
    cat_stats = []

    for (cat,) in categories:
        profession = PROFESSION_MAP.get(cat, "未分类")
        mat_count = db.query(func.count(Material.id)).filter(Material.category == cat).scalar() or 0
        quote_count = db.query(func.count(Quote.id)).join(Material).filter(
            Material.category == cat
        ).scalar() or 0

        avg_price_row = db.query(func.avg(Quote.unit_price)).join(Material).filter(
            Material.category == cat, Quote.unit_price > 0,
        ).scalar()

        supplier_count = db.query(func.count(func.distinct(Quote.supplier_id))).join(Material).filter(
            Material.category == cat
        ).scalar() or 0

        project_count = db.query(func.count(func.distinct(Quote.project_id))).join(Material).filter(
            Material.category == cat
        ).scalar() or 0

        baseline = compute_baseline(db, cat)
        cv = baseline.get("cv") if baseline.get("count", 0) > 0 else None

        cat_stats.append({
            "category": cat,
            "profession": profession,
            "total_materials": mat_count,
            "total_quotes": quote_count,
            "avg_price": round(avg_price_row, 2) if avg_price_row else None,
            "price_cv": round(cv, 3) if cv is not None else None,
            "supplier_count": supplier_count,
            "project_count": project_count,
        })

    return {
        "total_materials": total_materials,
        "total_suppliers": total_suppliers,
        "total_projects": total_projects,
        "total_quotes": total_quotes,
        "category_stats": cat_stats,
    }


def get_category_detail_stats(db: Session, category: str) -> dict:
    """Get detailed price statistics per sub-category."""
    profession = PROFESSION_MAP.get(category, "未分类")

    total_records = db.query(func.count(Material.id)).filter(
        Material.category == category
    ).scalar() or 0

    valid_prices = db.query(func.count(Quote.id)).join(Material).filter(
        Material.category == category, Quote.unit_price > 0,
    ).scalar() or 0

    sub_cats = db.query(Material.sub_category).filter(
        Material.category == category
    ).distinct().all()

    sub_stats = []
    for (sub_cat,) in sub_cats:
        if not sub_cat:
            sub_cat = "未分类"

        prices_q = db.query(Quote.unit_price).join(Material).filter(
            Material.category == category,
            Material.sub_category == sub_cat,
            Quote.unit_price > 0,
        )
        prices = [r[0] for r in prices_q.all()]
        if not prices:
            continue

        arr = np.array(prices)
        count = len(arr)
        mean_val = float(np.mean(arr))
        std_val = float(np.std(arr)) if count > 1 else 0.0
        cv_val = std_val / mean_val if mean_val > 0 else 0.0

        sub_stats.append({
            "sub_category": sub_cat,
            "count": count,
            "mean": round(mean_val, 2),
            "median": round(float(np.median(arr)), 2),
            "std": round(std_val, 2),
            "cv": round(cv_val, 3),
            "min": round(float(np.min(arr)), 2),
            "max": round(float(np.max(arr)), 2),
            "p10": round(float(np.percentile(arr, 10)), 2) if count >= 5 else round(float(np.min(arr)), 2),
            "p90": round(float(np.percentile(arr, 90)), 2) if count >= 5 else round(float(np.max(arr)), 2),
            "suggested_threshold": round(max(cv_val * 1.5, 0.05), 3),
        })

    sub_stats.sort(key=lambda x: -x["count"])

    return {
        "category": category,
        "profession": profession,
        "total_records": total_records,
        "valid_prices": valid_prices,
        "sub_categories": sub_stats,
    }


def refresh_material_baselines(db: Session, category: str | None = None):
    """Recompute ref_price fields for materials based on their quotes."""
    q = db.query(Material)
    if category:
        q = q.filter(Material.category == category)

    for mat in q.all():
        prices_q = db.query(Quote.unit_price).filter(
            Quote.material_id == mat.id, Quote.unit_price > 0,
        )
        prices = [r[0] for r in prices_q.all()]
        if not prices:
            continue

        arr = np.array(prices, dtype=float)
        n = len(arr)

        # IQR filtering
        q1, q3 = np.percentile(arr, [25, 75])
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        filtered = arr[(arr >= lower) & (arr <= upper)]
        if len(filtered) == 0:
            filtered = arr

        mean_val = float(np.mean(filtered))

        mat.ref_price_reasonable_low = float(np.min(filtered))   # 合理史低
        mat.ref_price_low = float(np.percentile(filtered, 10)) if n >= 5 else float(np.min(filtered))
        mat.ref_price_avg = mean_val
        mat.ref_price_median = float(np.median(filtered))
        mat.ref_price_high = float(np.percentile(filtered, 90)) if n >= 5 else float(np.max(filtered))
        mat.price_cv = float(np.std(filtered, ddof=1) / mean_val) if mean_val > 0 and len(filtered) > 1 else 0.0
        mat.deviation_threshold = round(max(mat.price_cv * 1.5, 0.05), 3)

        brands_q = db.query(func.distinct(Quote.brand)).filter(
            Quote.material_id == mat.id, Quote.brand != "", Quote.brand.isnot(None),
        )
        brands = [r[0] for r in brands_q.all() if r[0]]
        mat.recommended_brands = brands
        mat.supplier_count = len(brands)

    db.commit()


def get_dashboard_heatmap(
    db: Session,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """树状热力图：项目 → 品类 → 采购总金额。"""
    q = (
        db.query(
            Project.name,
            Material.category,
            func.sum(Quote.unit_price * func.coalesce(Quote.quantity, 1)).label("total"),
        )
        .join(Quote, Quote.project_id == Project.id)
        .join(Material, Quote.material_id == Material.id)
        .filter(Quote.unit_price > 0)
    )
    if date_from:
        q = q.filter(Quote.quote_date >= date_from)
    if date_to:
        q = q.filter(Quote.quote_date <= date_to)
    rows = q.group_by(Project.name, Material.category).all()

    project_map: dict[str, dict] = {}
    for proj_name, cat, total in rows:
        if not total:
            continue
        proj_name = proj_name or "未知项目"
        if proj_name not in project_map:
            project_map[proj_name] = {"name": proj_name, "value": 0.0, "children": {}}
        project_map[proj_name]["value"] += float(total)
        children = project_map[proj_name]["children"]
        children[cat] = children.get(cat, 0.0) + float(total)

    nodes = []
    for pname, pdata in project_map.items():
        children = [{"name": k, "value": round(v, 2)} for k, v in pdata["children"].items()]
        nodes.append({
            "name": pname,
            "value": round(pdata["value"], 2),
            "children": children,
        })
    nodes.sort(key=lambda x: -x["value"])
    return {"nodes": nodes}


def get_dashboard_bubble(db: Session) -> dict:
    """气泡图：品类 → 供应商 → 采购总金额。"""
    from apps.api.models import BrandTier

    rows = (
        db.query(
            Material.category,
            Supplier.name,
            func.sum(Quote.unit_price * func.coalesce(Quote.quantity, 1)).label("total"),
        )
        .join(Quote, Quote.material_id == Material.id)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .filter(Quote.unit_price > 0)
        .group_by(Material.category, Supplier.name)
        .all()
    )

    cat_map: dict[str, dict] = {}
    for cat, sup_name, total in rows:
        if not total:
            continue
        if cat not in cat_map:
            cat_map[cat] = {
                "name": cat,
                "profession": PROFESSION_MAP.get(cat, "其他"),
                "total_amount": 0.0,
                "children": [],
            }
        cat_map[cat]["total_amount"] += float(total)
        tier = None
        cat_map[cat]["children"].append({
            "name": sup_name or "未知供应商",
            "amount": round(float(total), 2),
            "tier": tier,
        })

    items = list(cat_map.values())
    for item in items:
        item["total_amount"] = round(item["total_amount"], 2)
        item["children"].sort(key=lambda x: -x["amount"])
    items.sort(key=lambda x: -x["total_amount"])
    return {"items": items}
