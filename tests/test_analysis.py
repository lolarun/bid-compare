"""Tests for analysis service logic — v2 (合理史低基准, normal/yellow/red 色标)."""

import numpy as np
from apps.api.models import Material, Quote
from apps.api.services.comparison import compute_baseline, determine_alert, compare_price
from apps.api.services.scoring import score_supplier
from apps.api.services.statistics import refresh_material_baselines


# ─── determine_alert tests (v2: normal/yellow/red only) ──────────────────────

def test_determine_alert_normal():
    thresholds = {"yellow": 0.05, "red": 0.10}
    assert determine_alert(0.03, thresholds) == "normal"
    assert determine_alert(-0.03, thresholds) == "normal"
    assert determine_alert(0.0, thresholds) == "normal"
    assert determine_alert(0.05, thresholds) == "normal"


def test_determine_alert_yellow():
    thresholds = {"yellow": 0.05, "red": 0.10}
    assert determine_alert(0.07, thresholds) == "yellow"
    assert determine_alert(0.10, thresholds) == "yellow"
    assert determine_alert(-0.07, thresholds) == "yellow"


def test_determine_alert_red():
    thresholds = {"yellow": 0.05, "red": 0.10}
    assert determine_alert(0.15, thresholds) == "red"
    assert determine_alert(0.50, thresholds) == "red"
    assert determine_alert(-0.15, thresholds) == "red"


def test_compute_baseline_empty(db_session):
    result = compute_baseline(db_session, "桥架")
    assert result["count"] == 0


def test_compute_baseline_with_data(db_session, sample_material, sample_quotes):
    result = compute_baseline(db_session, "桥架")
    assert result["count"] == 8
    assert result["mean"] > 0
    assert result["median"] > 0
    assert result["min"] <= result["median"] <= result["max"]
    assert "reasonable_low" in result
    assert result["reasonable_low"] <= result["mean"]


def test_compare_price_normal(db_session, sample_material, sample_quotes):
    result = compare_price(db_session, "桥架", new_price=50.0)
    assert result["sample_count"] == 8
    assert result["alert_level"] in ("normal", "yellow", "red")
    assert result["historical_avg"] is not None
    assert abs(result["deviation_pct"]) < 0.5  # within reason


def test_compare_price_high(db_session, sample_material, sample_quotes):
    result = compare_price(db_session, "桥架", new_price=200.0)
    assert result["alert_level"] == "red"
    assert result["deviation_pct"] > 0


def test_compare_price_low(db_session, sample_material, sample_quotes):
    # Very low price: deviation is negative, but still classified normally/yellow/red by abs threshold
    result = compare_price(db_session, "桥架", new_price=10.0)
    # deviation_pct will be negative (below reasonable_low), |dev| will be large → red
    assert result["alert_level"] in ("yellow", "red")
    assert result["deviation_pct"] < 0


def test_compare_price_no_data(db_session):
    result = compare_price(db_session, "水箱", new_price=5000.0)
    assert result["sample_count"] == 0
    assert result["alert_level"] == ""


def test_compare_price_returns_v2_fields(db_session, sample_material, sample_quotes):
    """Ensure v2 response schema: reasonable_low, historical_avg, etc."""
    result = compare_price(db_session, "桥架", new_price=50.0)
    assert "reasonable_low" in result
    assert "historical_avg" in result
    assert "historical_median" in result
    assert "historical_min" in result
    assert "baseline_high" in result
    assert "reasonable_low_project" in result
    assert "reasonable_low_date" in result


def test_score_supplier(db_session, sample_supplier, sample_material, sample_quotes):
    result = score_supplier(db_session, sample_supplier.id, "桥架")
    assert result["supplier_name"] == "测试供应商A"
    assert 0 <= result["total_score"] <= 100
    assert result["history_score"] == 80.0  # win_count=3 → 80


def test_score_supplier_weights_v2(db_session, sample_supplier, sample_material, sample_quotes):
    """Weights must use v2 keys: price/history/completeness/brand/commercial."""
    result = score_supplier(db_session, sample_supplier.id, "桥架")
    weights = result["weights"]
    assert "price" in weights
    assert "history" in weights
    assert "completeness" in weights
    assert "brand" in weights
    assert "commercial" in weights


def test_refresh_baselines(db_session, sample_material, sample_quotes):
    assert sample_material.ref_price_median == 50.0  # fixture default

    refresh_material_baselines(db_session, "桥架")
    db_session.refresh(sample_material)

    # After refresh, median should be recalculated from actual quotes (45,47,48,50,51,52,53,55)
    expected_median = float(np.median([45, 47, 48, 50, 51, 52, 53, 55]))
    assert abs(sample_material.ref_price_median - expected_median) < 0.5
    assert sample_material.price_cv > 0
    assert sample_material.deviation_threshold >= 0.05
    # v2: ref_price_reasonable_low should be set
    assert sample_material.ref_price_reasonable_low is not None
    assert sample_material.ref_price_reasonable_low <= sample_material.ref_price_median
