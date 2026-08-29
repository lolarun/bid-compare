"""Unit tests for tabular_ingestion.py — deterministic CSV/Excel quote extractor.

Test plan:
  1. QuoteFact.to_item_dict() key set == PDF item key set (contract guard)
  2. 3-row valve CSV (tax unit_price + qty, no total_price col) → derived totals,
     canonical.dn filled, _doc_meta.bid_total is None
  3. Same CSV + 价税合计 row → that row excluded from items, bid_total set,
     bid_total_basis="tax_included"; assess_readiness checksum "passed" when correct,
     "failed" when totals row intentionally wrong
  4. Explicit total_price col with >5% discrepancy → validation_warning set
  5. Multi-supplier column file → ValueError raised
  6. No name column / no header match → 0 items, no crash
"""
from __future__ import annotations

import csv
import io
import os
import tempfile

import pytest

# ─── QuoteFact key-set contract test (no file I/O needed) ─────────────────────

EXPECTED_ITEM_KEYS = {
    "material", "spec", "brand", "unit", "qty", "unit_price",
    "unit_price_excl_tax", "total_price", "tax_rate", "remark",
    "canonical", "validation_warning",
}


def test_quote_fact_to_item_dict_key_contract():
    """to_item_dict() must expose exactly the keys that batch-confirm expects.

    source_ref is optional/extra and must also be present when set.
    """
    from apps.api.intelligence.quote_fact import QuoteFact

    fact = QuoteFact(
        material="闸阀",
        spec="DN50",
        unit_price=100.0,
        qty=10.0,
    )
    d = fact.to_item_dict()
    # All required keys present
    assert EXPECTED_ITEM_KEYS.issubset(d.keys()), (
        f"Missing keys: {EXPECTED_ITEM_KEYS - d.keys()}"
    )
    # source_ref absent when not set
    assert "source_ref" not in d

    fact_with_ref = QuoteFact(
        material="截止阀",
        spec="DN25",
        source_ref={"sheet": "Sheet1", "row": 3},
    )
    d2 = fact_with_ref.to_item_dict()
    assert d2["source_ref"] == {"sheet": "Sheet1", "row": 3}


def test_quote_fact_does_not_silently_derive_total_price():
    """合并前审计（Fable复核，2026-08-09 quote_fact.py:129-134 的意图变更）：
    权威 total_price 在缺失时保持 None，不再被 qty*unit_price 静默覆盖——
    禁止未经确认自动改写原值。派生候选值改落 derived_total_candidate，
    total_source 标 missing，供入库门（doc/19 §L2）据此要求人工补写。
    这条测试原先断言的正是被有意移除的旧行为。"""
    from apps.api.intelligence.quote_fact import QuoteFact

    fact = QuoteFact(material="蝶阀", qty=5.0, unit_price=200.0)
    assert fact.total_price is None
    assert fact.derived_total_candidate == pytest.approx(1000.0)
    assert fact.total_source == "missing"


# ─── CSV helpers ───────────────────────────────────────────────────────────────

def _write_csv(rows: list[list[str]], *, encoding: str = "utf-8-sig") -> str:
    """Write rows to a named temp CSV file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)
    return path


VALVE_ROWS_HEADER = ["名称", "规格型号", "单位", "数量", "含税单价", "品牌", "备注"]
VALVE_ROWS_DATA = [
    ["闸阀",  "DN50 PN16", "个", "10", "100.00", "国产", ""],
    ["截止阀", "DN25 PN16", "个", "5",  "80.00",  "国产", "一般用途"],
    ["蝶阀",  "DN100 PN10", "个", "3",  "200.00", "进口", ""],
]


def test_basic_csv_no_total_row():
    """3 valve rows, no totals row → 3 items, canonical.dn set, bid_total None."""
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular

    path = _write_csv([VALVE_ROWS_HEADER] + VALVE_ROWS_DATA)
    try:
        result = extract_quote_tabular(path, {})
        items = result["items"]
        assert len(items) == 3, f"Expected 3, got {len(items)}"

        # 合并前审计（Fable复核）：total_price 不再被静默派生（2026-08-09
        # quote_fact.py:129-134），权威值缺失时保持 None；qty×单价的候选值
        # 改落 derived_total_candidate，不冒充已确认金额。
        for item, expected in zip(items, (1000.0, 400.0, 600.0)):
            assert item["total_price"] is None
            assert item["derived_total_candidate"] == pytest.approx(expected)
            assert item["total_source"] == "missing"

        # canonical should have dn extracted from spec
        for item in items:
            assert "dn" in item["canonical"], (
                f"Expected 'dn' in canonical for {item['material']}"
            )

        # doc_meta
        assert result["_doc_meta"]["bid_total"] is None
        assert result["_doc_meta"]["bid_total_basis"] == "unknown"
    finally:
        os.unlink(path)


def test_csv_with_tax_incl_total_row_checksum():
    """Totals row with 价税合计 → excluded from items, bid_total set, checksum works."""
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular
    from apps.api.services.submission.quote_readiness import assess_readiness

    computed = 10 * 100.0 + 5 * 80.0 + 3 * 200.0  # 1000+400+600 = 2000
    total_row = ["价税合计", "", "", "", str(computed), "", ""]

    path = _write_csv([VALVE_ROWS_HEADER] + VALVE_ROWS_DATA + [total_row])
    try:
        result = extract_quote_tabular(path, {})
        items = result["items"]
        assert len(items) == 3, "Totals row must be excluded from items"

        doc_meta = result["_doc_meta"]
        assert doc_meta["bid_total"] == pytest.approx(computed)
        assert doc_meta["bid_total_basis"] == "tax_included"

        # Checksum: computed_total == bid_total → passed
        stats = {"quote_rows": 3, "matched_rows": 3, "computed_total": computed}
        readiness = assess_readiness(
            supplier_id=1,
            supplier_name="测试供应商",
            stats=stats,
            doc_meta=doc_meta,
        )
        assert readiness.checksum_status == "passed", (
            f"Expected 'passed', got {readiness.checksum_status}"
        )

        # Now use a wrong total → checksum failed
        wrong_total = computed * 1.10  # +10% intentional error
        doc_meta_wrong = dict(doc_meta, bid_total=wrong_total)
        readiness_fail = assess_readiness(
            supplier_id=1,
            supplier_name="测试供应商",
            stats=stats,
            doc_meta=doc_meta_wrong,
        )
        assert readiness_fail.checksum_status == "failed", (
            f"Expected 'failed', got {readiness_fail.checksum_status}"
        )
    finally:
        os.unlink(path)


def test_validation_warning_on_arithmetic_mismatch():
    """Explicit total_price column that disagrees with qty×unit_price → validation_warning."""
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular

    # Header includes a total_price column that we will fill inconsistently
    # import_service._COLUMN_PATTERNS doesn't have a direct "total_price" key,
    # so we bake the mismatch into the per-row total via indirect means:
    # Actually the tabular_ingestion derives total_price from qty*unit_price,
    # then apply_arithmetic_validation checks it.  To trigger the warning we
    # need an EXPLICIT合价 column with a wrong value.
    # We add a column "含税合价" (price col pattern would match "含税单价" first,
    # but let's use 合价 which matches "price" pattern via "合价").
    # Actually checking _COLUMN_PATTERNS: "price": [r"含税单价", r"含税.*单价", ...]
    # "合价" is NOT in _COLUMN_PATTERNS, so we can't inject a wrong total via
    # column mapping directly.  Instead, supply unit_price=100, qty=10,
    # and we'll manually set total_price by adding QuoteFact with explicit total
    # that differs >5% — test this via quote_fact directly.
    from apps.api.intelligence.quote_fact import QuoteFact, apply_arithmetic_validation

    items = [
        QuoteFact(
            material="闸阀", spec="DN50", qty=10.0, unit_price=100.0,
            total_price=900.0,  # wrong: should be 1000, diff=10%
        ).to_item_dict()
    ]
    apply_arithmetic_validation(items)
    assert items[0]["validation_warning"] != "", (
        "Expected validation_warning for qty×price ≠ total"
    )
    assert "金额不符" in items[0]["validation_warning"]


def test_multi_supplier_column_raises():
    """CSV with supplier column containing >1 unique supplier → ValueError."""
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular

    header = ["名称", "规格型号", "单位", "数量", "含税单价", "供应商"]
    rows = [
        ["闸阀",  "DN50",  "个", "10", "100.00", "供应商A"],
        ["截止阀", "DN25",  "个", "5",  "80.00",  "供应商B"],
    ]
    path = _write_csv([header] + rows)
    try:
        with pytest.raises(ValueError, match="多供应商"):
            extract_quote_tabular(path, {})
    finally:
        os.unlink(path)


def test_no_name_column_raises():
    """CSV with no recognisable name column → ValueError (hard fail, not silent empty)."""
    from apps.api.services.ingestion.tabular_ingestion import extract_quote_tabular

    header = ["列A", "列B", "列C"]
    rows = [
        ["100", "200", "300"],
        ["400", "500", "600"],
    ]
    path = _write_csv([header] + rows)
    try:
        with pytest.raises(ValueError, match="未识别到物料名称列"):
            extract_quote_tabular(path, {})
    finally:
        os.unlink(path)


def test_column_detection_separates_unit_price_from_total_price():
    """价税合计 column header must map to total_price, NOT unit_price."""
    from apps.api.services.ingestion.tabular_ingestion import _detect_tabular_columns

    # Typical comparison-sheet columns
    cols = ["名称", "规格型号", "单位", "数量", "含税单价", "不含税单价", "价税合计"]
    col_map = _detect_tabular_columns(cols)

    assert col_map.get("name") == "名称"
    assert col_map.get("unit_price") == "含税单价", (
        f"unit_price should be '含税单价', got {col_map.get('unit_price')!r}"
    )
    assert col_map.get("total_price") == "价税合计", (
        f"total_price should be '价税合计', got {col_map.get('total_price')!r}"
    )


def test_column_detection_incl_excl_tax_both_orders():
    """含税单价 / 不含税单价 must map to distinct columns regardless of order.

    Regression for the substring bug: "不含税单价" contains "含税单价", so a naive
    pattern would let unit_price grab the excl-tax column when it appears first.
    """
    from apps.api.services.ingestion.tabular_ingestion import _detect_tabular_columns

    # excl-tax column FIRST (the order that triggered the bug)
    m1 = _detect_tabular_columns(["名称", "规格型号", "数量", "不含税单价", "含税单价"])
    assert m1.get("unit_price") == "含税单价", f"got {m1.get('unit_price')!r}"
    assert m1.get("unit_price_excl_tax") == "不含税单价", f"got {m1.get('unit_price_excl_tax')!r}"

    # incl-tax column FIRST
    m2 = _detect_tabular_columns(["名称", "规格型号", "数量", "含税单价", "不含税单价"])
    assert m2.get("unit_price") == "含税单价", f"got {m2.get('unit_price')!r}"
    assert m2.get("unit_price_excl_tax") == "不含税单价", f"got {m2.get('unit_price_excl_tax')!r}"


def test_column_detection_lone_excl_tax_does_not_fill_unit_price():
    """A file with ONLY 不含税单价 must not populate unit_price (incl-tax)."""
    from apps.api.services.ingestion.tabular_ingestion import _detect_tabular_columns

    m = _detect_tabular_columns(["名称", "规格型号", "数量", "不含税单价"])
    assert m.get("unit_price_excl_tax") == "不含税单价"
    assert m.get("unit_price") is None, (
        f"lone 不含税单价 must not fill unit_price, got {m.get('unit_price')!r}"
    )


def test_parse_number_handles_thousand_separators():
    """_parse_number must handle Chinese supplier number formatting."""
    from apps.api.services.ingestion.tabular_ingestion import _parse_number

    assert _parse_number("1,234.56") == pytest.approx(1234.56)
    assert _parse_number("¥1,234.56") == pytest.approx(1234.56)
    assert _parse_number("￥98,765.00") == pytest.approx(98765.0)
    assert _parse_number("100元") == pytest.approx(100.0)
    assert _parse_number(None) is None
    assert _parse_number("") is None
    assert _parse_number("nan") is None


def test_build_canonical_shared_helper():
    """build_canonical merges code-extracted with LLM-extracted dict (LLM wins)."""
    from apps.api.intelligence.quote_fact import build_canonical

    # Code extraction should find DN50 from spec (stored as "DN50" or "50" per canonical.py)
    canon = build_canonical("闸阀", "DN50 PN16")
    dn_val = canon.get("dn")
    assert dn_val is not None, f"Expected 'dn' key in canonical, got {canon}"
    assert "50" in str(dn_val), f"Expected dn to contain '50', got {dn_val!r}"

    # LLM override: supply dn=65 which beats code-extracted value
    canon_llm = build_canonical("闸阀", "DN50 PN16", llm_canonical={"dn": "65"})
    assert canon_llm["dn"] == "65", "LLM value should override code-extracted value"


def test_build_canonical_material_type_feeds_material():
    """material_type (材质) must populate canonical.material."""
    from apps.api.intelligence.quote_fact import build_canonical

    canon = build_canonical("球阀", "DN50 PN16", material_type="不锈钢")
    assert canon.get("material") == "不锈钢", f"Expected material=不锈钢, got {canon}"


def test_quote_fact_to_item_dict_includes_material_type():
    """to_item_dict() must carry material_type for the full-chain wiring."""
    from apps.api.intelligence.quote_fact import quote_fact_from_row

    fact = quote_fact_from_row(material="球阀", spec="DN50", material_type="球墨铸铁")
    d = fact.to_item_dict()
    assert d["material_type"] == "球墨铸铁"
    assert d["canonical"].get("material") == "球墨铸铁"
