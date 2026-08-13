"""Price comparison and baseline computation service — v2.

Key changes vs v1:
- Baseline is now "reasonable_low" (min of IQR-filtered prices), not median.
- Alert system is 3-level: normal/yellow/red  (no green/blue).
- Thresholds config uses {yellow, red} keys, not {tolerance, yellow, red}.
"""

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Project, Supplier, AnalysisConfig, DEFAULT_THRESHOLDS
from apps.api.services.ingestion.canonical import extract_valve_canonical, normalize_valve_family
from apps.api.services.history.quote_filters import valid_quote_filters
from apps.api.services.history.comparison_profiles import get_comparison_profile

# 同规格基准最小样本数（< 此值视为"无可靠同规格基准"，deviation=null，不计异常）
SPEC_BASELINE_MIN_SAMPLES = 5


def get_config_value(db: Session, key: str, default=None):
    cfg = db.scalar(select(AnalysisConfig).where(AnalysisConfig.key == key))
    return cfg.value if cfg else default


def get_category_thresholds(db: Session, category: str) -> dict:
    """Return {yellow, red} thresholds for a category, falling back to default."""
    thresholds_map = get_config_value(db, "thresholds", DEFAULT_THRESHOLDS)
    return thresholds_map.get(category) or thresholds_map.get("default", {"yellow": 0.05, "red": 0.10})


def determine_alert(deviation_pct: float, thresholds: dict) -> str:
    """Classify deviation into normal/yellow/red using {yellow, red} thresholds."""
    yellow = thresholds.get("yellow", 0.05)
    red = thresholds.get("red", 0.10)
    abs_dev = abs(deviation_pct)
    if abs_dev <= yellow:
        return "normal"
    if abs_dev <= red:
        return "yellow"
    return "red"


def compute_baseline(db: Session, category: str, sub_category: str | None = None,
                     brand_tier: str | None = None) -> dict:
    """Compute IQR-filtered price statistics for a category/sub_category.

    brand_tier: if set, only include quotes with matching brand_tier (e.g. '合资').
    """
    stmt = (
        select(Quote.unit_price)
        .join(Material)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .where(
            Material.category == category,
            Quote.unit_price.isnot(None),
            Quote.unit_price > 0,
            *valid_quote_filters(),
        )
    )
    if sub_category:
        stmt = stmt.where(Material.sub_category == sub_category)
    if brand_tier:
        stmt = stmt.where(Quote.brand_tier == brand_tier)

    prices = list(db.scalars(stmt).all())
    if not prices:
        return {"count": 0}

    arr = np.array(prices, dtype=float)
    q1, q3 = np.percentile(arr, [25, 75])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    filtered = arr[(arr >= lower) & (arr <= upper)]

    if len(filtered) == 0:
        filtered = arr

    n = len(filtered)
    mean_val = float(np.mean(filtered))
    std_val = float(np.std(filtered, ddof=1)) if n > 1 else 0.0
    cv_val = std_val / mean_val if mean_val > 0 else 0.0

    return {
        "count": len(prices),
        "filtered_count": n,
        "mean": mean_val,
        "median": float(np.median(filtered)),
        "std": std_val,
        "cv": cv_val,
        "p10": float(np.percentile(filtered, 10)) if n >= 5 else float(np.min(filtered)),
        "p90": float(np.percentile(filtered, 90)) if n >= 5 else float(np.max(filtered)),
        "min": float(np.min(filtered)),
        "max": float(np.max(filtered)),
        "iqr_lower": float(lower),
        "iqr_upper": float(upper),
        "reasonable_low": float(np.min(filtered)),   # 合理史低 = IQR过滤后最小值
        "historical_min": float(np.min(arr)),         # 绝对最低（仅提醒）
    }


def compute_reasonable_low(
    db: Session,
    category: str,
    sub_category: str | None = None,
    brand_tier: str | None = None,
) -> dict:
    """
    Compute the reasonable low price (合理史低) with its source project and date.

    brand_tier: if set, only include quotes with matching brand_tier (e.g. '合资').
    """
    stmt = (
        select(Quote.unit_price, Quote.quote_date, Quote.project_id)
        .join(Material)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .where(
            Material.category == category,
            Quote.unit_price.isnot(None),
            Quote.unit_price > 0,
            *valid_quote_filters(),
        )
    )
    if sub_category:
        stmt = stmt.where(Material.sub_category == sub_category)
    if brand_tier:
        stmt = stmt.where(Quote.brand_tier == brand_tier)

    rows = db.execute(stmt).all()
    if not rows:
        return {
            "reasonable_low": None,
            "reasonable_low_project": None,
            "reasonable_low_date": None,
            "historical_min": None,
        }

    prices = np.array([r[0] for r in rows], dtype=float)
    q1, q3 = np.percentile(prices, [25, 75])
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    filtered_rows = [(r[0], r[1], r[2]) for r in rows if lower <= r[0] <= upper]

    if not filtered_rows:
        filtered_rows = list(rows)

    # 合理史低 = 过滤后最小价的那条记录
    min_row = min(filtered_rows, key=lambda x: x[0])
    min_price, min_date, min_project_id = min_row

    project_name = None
    if min_project_id:
        proj = db.get(Project, min_project_id)
        project_name = proj.name if proj else None

    return {
        "reasonable_low": float(min_price),
        "reasonable_low_project": project_name,
        "reasonable_low_date": min_date or "",
        "historical_min": float(np.min(prices)),
    }


# ─── 同规格历史基准（合理低价偏差的唯一合法基准）─────────────────────────────
#
# 偏差只能相对**同规格**历史中位数计算：键 = valve_family + DN + PN + unit + tax_basis。
# 规格键不全 / 税口径未知 / 样本 < SPEC_BASELINE_MIN_SAMPLES → 返回 None
# （前端显示"无可靠同规格基准"，deviation 必须为 null，绝不计异常）。
# 严禁退回品类最低价 / P10 / 跨规格地板价（那正是 ¥11.10 假异常的根因）。
# 历史报价必须同税口径才纳样（含税价用 unit_price 且需证据为含税口径；不含税用 unit_price_excl_tax），
# 历史无可确认口径的样本一律排除——宁缺毋滥，避免含税/不含税混样。


def _spec_key(name: str, spec: str, unit: str) -> tuple:
    """(valve_family, dn, pn, unit) — 同规格匹配键。任一缺失则该项不可用于同规格基准。"""
    c = extract_valve_canonical(name or "", spec or "")
    fam = normalize_valve_family(c.get("valve_type"))
    return (fam, c.get("dn"), c.get("pn"), (unit or "").strip())


def build_spec_price_index(db: Session, category: str) -> dict[tuple, list[float]]:
    """一次扫描该品类历史报价，建 (family,dn,pn,unit,tax_basis) → [价格] 索引。

    每行矩阵按自身规格键 O(1) 查表，避免 N×全表重复扫描。
    """
    rows = db.execute(
        select(
            Quote.unit_price, Quote.unit_price_excl_tax, Quote.tax_rate,
            Material.standard_name, Material.spec, Material.unit,
        )
        .join(Material, Material.id == Quote.material_id)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .where(Material.category == category, *valid_quote_filters())
    ).all()
    index: dict[tuple, list[float]] = {}
    for up, upx, tr, name, spec, munit in rows:
        fam, dn, pn, u = _spec_key(name, spec, munit)
        if not (fam and dn and pn and u):
            continue
        # 含税口径样本：unit_price 且能确认为含税（含税>不含税，或有正税率）
        if up and up > 0 and ((upx and up > upx) or (tr and tr > 0)):
            index.setdefault((fam, dn, pn, u, "incl_tax"), []).append(float(up))
        # 不含税口径样本
        if upx and upx > 0:
            index.setdefault((fam, dn, pn, u, "excl_tax"), []).append(float(upx))
    return index


def spec_baseline_from_index(
    index: dict[tuple, list[float]],
    valve_family: str | None,
    dn: str | None,
    pn: str | None,
    unit: str | None,
    tax_basis: str | None,
    min_samples: int = SPEC_BASELINE_MIN_SAMPLES,
) -> dict | None:
    """从索引取同规格基准。规格键不全 / 税口径未知 / 样本不足 → None。

    返回 {median, count, basis, spec_key}；median 同时是展示值与偏差计算基准（同源）。
    """
    u = (unit or "").strip()
    if not (valve_family and dn and pn and u) or tax_basis not in ("incl_tax", "excl_tax"):
        return None
    prices = index.get((valve_family, dn, pn, u, tax_basis))
    if not prices or len(prices) < min_samples:
        return None
    median = float(np.median(prices))
    return {
        "median": round(median, 2),
        "count": len(prices),
        "basis": tax_basis,
        "spec_key": f"{valve_family}|{dn}|{pn}|{u}|{tax_basis}",
    }


def compute_spec_baseline(
    db: Session,
    category: str,
    valve_family: str | None,
    dn: str | None,
    pn: str | None,
    unit: str | None,
    tax_basis: str | None,
    min_samples: int = SPEC_BASELINE_MIN_SAMPLES,
) -> dict | None:
    """便捷封装（建索引 + 查表）；矩阵路径请用 build_spec_price_index 复用索引。"""
    index = build_spec_price_index(db, category)
    return spec_baseline_from_index(index, valve_family, dn, pn, unit, tax_basis, min_samples)


def compare_price(
    db: Session,
    category: str,
    sub_category: str | None = None,
    new_price: float | None = None,
) -> dict:
    """Compare a new price against the reasonable low baseline (v2)."""
    profile = get_comparison_profile(category)
    if not profile["history_baseline"]:
        return {
            "category": category,
            "sub_category": sub_category or "",
            "reasonable_low": None,
            "reasonable_low_project": None,
            "reasonable_low_date": None,
            "historical_avg": None,
            "historical_median": None,
            "historical_min": None,
            "baseline_high": None,
            "new_price": new_price,
            "deviation_pct": None,
            "alert_level": "",
            "sample_count": 0,
            "comparison_profile": profile["key"],
            "review_hint": profile["review_hint"],
        }
    baseline = compute_baseline(db, category, sub_category)
    rl_info = compute_reasonable_low(db, category, sub_category)

    if baseline.get("count", 0) == 0:
        return {
            "category": category,
            "sub_category": sub_category or "",
            "reasonable_low": None,
            "reasonable_low_project": None,
            "reasonable_low_date": None,
            "historical_avg": None,
            "historical_median": None,
            "historical_min": None,
            "baseline_high": None,
            "new_price": new_price,
            "deviation_pct": None,
            "alert_level": "",
            "sample_count": 0,
            "comparison_profile": profile["key"],
            "review_hint": profile["review_hint"],
        }

    thresholds = get_category_thresholds(db, category)
    reasonable_low = rl_info["reasonable_low"]
    deviation_pct = None
    alert_level = ""

    if new_price is not None and reasonable_low and reasonable_low > 0:
        deviation_pct = round((new_price - reasonable_low) / reasonable_low, 4)
        alert_level = determine_alert(deviation_pct, thresholds)

    return {
        "category": category,
        "sub_category": sub_category or "",
        "reasonable_low": round(reasonable_low, 2) if reasonable_low is not None else None,
        "reasonable_low_project": rl_info["reasonable_low_project"],
        "reasonable_low_date": rl_info["reasonable_low_date"],
        "historical_avg": round(baseline["mean"], 2),
        "historical_median": round(baseline["median"], 2),
        "historical_min": round(rl_info["historical_min"], 2) if rl_info["historical_min"] is not None else None,
        "baseline_high": round(baseline["p90"], 2),
        "new_price": new_price,
        "deviation_pct": deviation_pct,
        "alert_level": alert_level,
        "sample_count": baseline["count"],
        "comparison_profile": profile["key"],
        "review_hint": profile["review_hint"],
    }
