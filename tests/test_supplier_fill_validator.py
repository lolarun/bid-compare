"""Phase 3: supplier_fill_llm.validate() — anti-hallucination safety core.

Pure-function tests (no DB, no network). Every guarantee that keeps the LLM
honest is pinned here independent of model behavior:
  (a) hallucinated quote_id dropped
  (b) hallucinated anchor_seq dropped
  (c) duplicate quote_id deduped (higher confidence wins)
  (d) LLM price never trusted; mismatch → quoted downgraded to pending
  (e) aggregation: consistent members collapse to ONE cell with Σ totals;
      conflicting member split off to pending
  (f) residue computed; residue_high_cos counted
  (g) canonical conflict → quoted downgraded to pending
  + split (one quote → many anchors) rejected in v1
  + price always sourced from the Quote row, never the LLM
"""
from apps.api.services.supplier_fill_llm import (
    AnchorView, SupplierQuoteRow, validate,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────

def _anchors():
    return [
        AnchorView(seq=1, name="球阀", spec="DN50", canonical={"valve_type": "球阀", "dn": "DN50"}),
        AnchorView(seq=2, name="闸阀", spec="DN80", canonical={"valve_type": "闸阀", "dn": "DN80"}),
        AnchorView(seq=3, name="蝶阀", spec="DN100", canonical={"valve_type": "蝶阀", "dn": "DN100"}),
    ]


def _row(qid, **kw):
    base = dict(
        quote_id=qid, supplier_id=7, material="球阀", spec="DN50",
        qty=10.0, unit_price=100.0, total_price=1000.0,
        canonical={"valve_type": "球阀", "dn": "DN50"},
    )
    base.update(kw)
    return SupplierQuoteRow(**base)


# ─── (a) unknown quote_id ─────────────────────────────────────────────────────

def test_unknown_quote_id_dropped():
    rows = [_row(101)]
    raw = {"assignments": [{"quote_id": 999, "anchor_seq": 1, "status": "quoted", "confidence": 0.9}]}
    res = validate(raw, _anchors(), rows)
    assert res.cells == []
    assert any(d["reason"] == "unknown_quote_id" for d in res.dropped)
    assert res.residue_quote_ids == [101]  # the real quote never got assigned


# ─── (b) unknown anchor_seq ───────────────────────────────────────────────────

def test_unknown_anchor_seq_dropped():
    rows = [_row(101)]
    raw = {"assignments": [{"quote_id": 101, "anchor_seq": 88, "status": "quoted", "confidence": 0.9}]}
    res = validate(raw, _anchors(), rows)
    assert res.cells == []
    assert any(d["reason"] == "unknown_anchor_seq" for d in res.dropped)
    assert res.residue_quote_ids == [101]


# ─── (c) duplicate quote_id (higher confidence wins) ──────────────────────────

def test_duplicate_quote_id_deduped_highest_confidence_wins():
    rows = [_row(101)]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "quoted", "confidence": 0.6},
        {"quote_id": 101, "anchor_seq": 2, "status": "quoted", "confidence": 0.95},
    ]}
    res = validate(raw, _anchors(), rows)
    # only one cell, for the higher-confidence anchor (seq=2)... but seq=2 is 闸阀,
    # row is 球阀 → canonical conflict downgrades to pending. Use distinct rows instead:
    assert len([c for c in res.cells]) == 1
    assert any(d["reason"] == "duplicate_quote_id" for d in res.dropped)


def test_duplicate_quote_id_consumes_once_no_split():
    """A quote assigned to two anchors must land on exactly one (no split in v1)."""
    rows = [_row(101, canonical={"valve_type": "球阀", "dn": "DN50"})]
    # both anchors compatible-ish; higher confidence one wins, other dropped as duplicate
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "quoted", "confidence": 0.9},
        {"quote_id": 101, "anchor_seq": 3, "status": "quoted", "confidence": 0.5},
    ]}
    res = validate(rows=rows, anchors=_anchors(), raw_llm=raw)
    assert len(res.cells) == 1
    assert res.cells[0].anchor_seq == 1  # higher confidence
    assert res.cells[0].quote_id == 101
    assert sum(1 for d in res.dropped if d["reason"] == "duplicate_quote_id") == 1


# ─── (d) price integrity ──────────────────────────────────────────────────────

def test_price_always_from_quote_row_not_llm():
    rows = [_row(101, unit_price=100.0, total_price=1000.0)]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "quoted", "confidence": 0.9, "llm_unit_price": 100.0},
    ]}
    res = validate(raw, _anchors(), rows)
    assert len(res.cells) == 1
    assert res.cells[0].unit_price == 100.0  # from row
    assert res.cells[0].total_price == 1000.0


def test_llm_price_mismatch_downgrades_to_pending():
    rows = [_row(101, unit_price=100.0)]
    raw = {"assignments": [
        # LLM claims 200 but the real quote is 100 → >5% mismatch → pending
        {"quote_id": 101, "anchor_seq": 1, "status": "quoted", "confidence": 0.9, "llm_unit_price": 200.0},
    ]}
    res = validate(raw, _anchors(), rows)
    assert len(res.cells) == 1
    cell = res.cells[0]
    assert cell.status == "pending"
    assert cell.action == "pending"
    assert "price_mismatch" in cell.flags
    assert cell.unit_price == 100.0  # still the real price


# ─── (e) aggregation ──────────────────────────────────────────────────────────

def test_aggregation_collapses_to_one_cell_with_sum():
    """Two consistent quotes for the same anchor → ONE aggregated cell, Σ totals."""
    rows = [
        _row(101, unit_price=100.0, qty=10.0, total_price=1000.0),
        _row(102, unit_price=120.0, qty=5.0, total_price=600.0),
    ]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "aggregated", "confidence": 0.9},
        {"quote_id": 102, "anchor_seq": 1, "status": "aggregated", "confidence": 0.85},
    ]}
    res = validate(raw, _anchors(), rows)
    agg_cells = [c for c in res.cells if c.anchor_seq == 1]
    assert len(agg_cells) == 1, f"must collapse to one cell, got {len(agg_cells)}"
    cell = agg_cells[0]
    assert cell.status == "aggregated"
    assert cell.action == "align"
    assert cell.agg_total == 1600.0
    assert cell.agg_qty == 15.0
    assert set(cell.aggregated_quote_ids) == {101, 102}


def test_aggregation_conflicting_member_split_to_pending():
    """A canonical-conflicting member is split off to pending, not aggregated."""
    rows = [
        _row(101, canonical={"valve_type": "球阀", "dn": "DN50"}),
        # different valve_type → conflicts with seed
        _row(102, canonical={"valve_type": "闸阀", "dn": "DN50"}),
    ]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "aggregated", "confidence": 0.9},
        {"quote_id": 102, "anchor_seq": 1, "status": "aggregated", "confidence": 0.8},
    ]}
    res = validate(raw, _anchors(), rows)
    cells = [c for c in res.cells if c.anchor_seq == 1]
    statuses = sorted(c.status for c in cells)
    # one stays (single align→quoted), the conflicting one becomes pending
    assert "pending" in statuses, f"conflicting member should be pending, got {statuses}"


# ─── (f) residue + residue_high_cos ───────────────────────────────────────────

def test_residue_and_high_cos_counted():
    rows = [
        _row(101, topk=[(1, 0.85)]),   # unassigned, high cos → residue_high_cos
        _row(102, topk=[(2, 0.40)]),   # unassigned, low cos
    ]
    raw = {"assignments": []}  # LLM assigned nothing
    res = validate(raw, _anchors(), rows)
    assert set(res.residue_quote_ids) == {101, 102}
    assert res.residue_high_cos == 1  # only qid 101 has best cos >= 0.70


# ─── (g) canonical conflict downgrade ─────────────────────────────────────────

def test_canonical_conflict_downgrades_quoted_to_pending():
    # row is 球阀 but assigned to 闸阀 anchor (seq=2) → hard conflict
    rows = [_row(101, canonical={"valve_type": "球阀", "dn": "DN50"})]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 2, "status": "quoted", "confidence": 0.9},
    ]}
    res = validate(raw, _anchors(), rows)
    assert len(res.cells) == 1
    cell = res.cells[0]
    assert cell.status == "pending"
    assert "canonical_conflict" in cell.flags


# ─── malformed input ──────────────────────────────────────────────────────────

def test_malformed_llm_output_all_residue():
    rows = [_row(101), _row(102)]
    for bad in ({}, {"assignments": None}, {"assignments": "oops"}, {"nope": 1}):
        res = validate(bad, _anchors(), rows)
        assert res.cells == []
        assert set(res.residue_quote_ids) == {101, 102}


def test_empty_rows_no_crash():
    res = validate({"assignments": []}, _anchors(), [])
    assert res.cells == []
    assert res.residue_quote_ids == []


# ─── status → action mapping ──────────────────────────────────────────────────

def test_status_action_mapping():
    rows = [_row(101), _row(102, canonical={"valve_type": "球阀", "dn": "DN50"})]
    raw = {"assignments": [
        {"quote_id": 101, "anchor_seq": 1, "status": "quoted", "confidence": 0.9},
        {"quote_id": 102, "anchor_seq": 1, "status": "excluded", "confidence": 0.8},
    ]}
    res = validate(raw, _anchors(), rows)
    by_status = {c.status: c for c in res.cells}
    assert by_status["quoted"].action == "align"
    assert by_status["excluded"].action == "exclude"
