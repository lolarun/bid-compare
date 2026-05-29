"""Price comparison and baseline computation service — v2.

Key changes vs v1:
- Baseline is now "reasonable_low" (min of IQR-filtered prices), not median.
- Alert system is 3-level: normal/yellow/red  (no green/blue).
- Thresholds config uses {yellow, red} keys, not {tolerance, yellow, red}.
"""

import numpy as np
from sqlalchemy.orm import Session

from apps.api.models import Material, Quote, Project, AnalysisConfig, DEFAULT_THRESHOLDS


def get_config_value(db: Session, key: str, default=None):
    cfg = db.query(AnalysisConfig).filter(AnalysisConfig.key == key).first()
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
    q = db.query(Quote.unit_price).join(Material).filter(
        Material.category == category,
        Quote.unit_price.isnot(None),
        Quote.unit_price > 0,
    )
    if sub_category:
        q = q.filter(Material.sub_category == sub_category)
    if brand_tier:
        q = q.filter(Quote.brand_tier == brand_tier)

    prices = [row[0] for row in q.all()]
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
    q = (
        db.query(Quote.unit_price, Quote.quote_date, Quote.project_id)
        .join(Material)
        .filter(
            Material.category == category,
            Quote.unit_price.isnot(None),
            Quote.unit_price > 0,
        )
    )
    if sub_category:
        q = q.filter(Material.sub_category == sub_category)
    if brand_tier:
        q = q.filter(Quote.brand_tier == brand_tier)

    rows = q.all()
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


def compare_price(
    db: Session,
    category: str,
    sub_category: str | None = None,
    new_price: float | None = None,
) -> dict:
    """Compare a new price against the reasonable low baseline (v2)."""
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
    }
