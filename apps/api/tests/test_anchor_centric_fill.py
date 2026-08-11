"""Unit tests for anchor-centric LLM fill (validate_anchor_fill).

Covers the six scenarios from the Wave 2 spec:
1. OCR correction: 阀阀→闸阀 resolves canonical conflict → cell quoted
2. OCR correction: 橡胶海→橡胶瓣止回阀 DN50 → cell quoted
3. missing decision → no cell, nearest_quote_candidates in dropped
4. unknown quote_id → dropped, fill downgrades to pending
5. same anchor multiple quote_ids → aggregated cell
6. canonical conflict without OCR correction → downgraded to pending
"""
from __future__ import annotations

import pytest

from apps.api.services.alignment.anchor_match import attach_nearest_hints, QuoteCandidate
from apps.api.services.supplier.supplier_fill_llm import (
    AnchorView,
    SupplierQuoteRow,
    validate_anchor_fill,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _row(quote_id: int, material: str = "", spec: str = "", canonical: dict | None = None,
         unit_price: float = 100.0, qty: float = 1.0, normalized_material: str = "") -> SupplierQuoteRow:
    return SupplierQuoteRow(
        quote_id=quote_id,
        supplier_id=1,
        raw_material=material,
        raw_spec=spec,
        spec=spec,
        material=material,
        normalized_material=normalized_material,
        unit_price=unit_price,
        qty=qty,
        total_price=unit_price * qty,
        canonical=canonical or {},
    )


def _anchor(seq: int, name: str = "", spec: str = "", canonical: dict | None = None) -> AnchorView:
    return AnchorView(seq=seq, name=name, spec=spec, canonical=canonical or {})


def _llm(fills: list[dict]) -> dict:
    return {"fills": fills}


# ── Test 1: OCR correction 阀阀→闸阀 resolves conflict ────────────────────────

class TestOcrCorrectionGateAllow:
    def test_valve_valve_corrected_to_gate_valve_is_quoted(self):
        """阀阀 DN65 with ocr_correction→闸阀 should bypass canonical conflict."""
        rows = [_row(101, material="阀阀", spec="DN65",
                     canonical={"valve_type": "截止阀", "dn": "DN65"},
                     normalized_material="闸阀")]
        anchors = [_anchor(1, name="闸阀", spec="DN65",
                           canonical={"valve_type": "闸阀", "dn": "DN65"})]
        raw = _llm([{
            "anchor_seq": 1,
            "decision": "quoted",
            "confidence": 0.92,
            "quote_ids": [101],
            "evidence": "DN65规格一致，阀阀为OCR误识别",
            "ocr_correction": {"from": "阀阀", "to": "闸阀"},
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "quoted"
        assert "ocr_corrected" in cell.flags
        assert "canonical_conflict" not in cell.flags
        assert cell.quote_id == 101
        assert cell.anchor_seq == 1


# ── Test 2: OCR correction 橡胶海止回阀→橡胶瓣止回阀 DN50 ───────────────────

class TestOcrCorrectionRubberFlap:
    def test_rubber_sea_corrected_to_rubber_flap_check_valve(self):
        """橡胶海止回阀 DN50 with plausible OCR correction→橡胶瓣止回阀 must produce quoted cell.

        The raw text 'normalized_material' is still the OCR error (as it is in production DB).
        The g2 gate would extract valve_type='止回阀' from the raw text and conflict with
        anchor_vt='橡胶瓣止回阀' — but the OCR correction path should bypass this because
        '止回阀' is a parent class of '橡胶瓣止回阀' (止回阀 ∈ 橡胶瓣止回阀).
        """
        rows = [_row(201, material="橡胶海止回阀", spec="DN50",
                     canonical={},  # stale: OCR error → blank canonical
                     normalized_material="橡胶海止回阀")]  # still the error in DB
        anchors = [_anchor(5, name="橡胶瓣止回阀", spec="DN50",
                           canonical={"valve_type": "橡胶瓣止回阀", "dn": "DN50"})]
        raw = _llm([{
            "anchor_seq": 5,
            "decision": "quoted",
            "confidence": 0.88,
            "quote_ids": [201],
            "evidence": "橡胶海为橡胶瓣OCR形近字错误，DN50一致",
            "ocr_correction": {"from": "橡胶海止回阀", "to": "橡胶瓣止回阀"},
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "quoted", f"Expected quoted, got {cell.status} flags={cell.flags}"
        assert "ocr_corrected" in cell.flags
        assert "ocr_corrected_verified" in cell.flags
        assert "valve_type_conflict" not in " ".join(cell.flags)
        assert cell.anchor_seq == 5


# ── Test 3: missing decision → no cell, evidence in dropped ──────────────────

class TestMissingDecision:
    def test_missing_produces_no_cell_and_logs_dropped(self):
        """LLM decision=missing must log nearest_quote_candidates, produce no cell."""
        rows = [_row(301, material="球墨铸铁闸阀", spec="DN100")]
        anchors = [_anchor(10, name="球墨铸铁闸阀", spec="DN100",
                           canonical={"valve_type": "闸阀", "dn": "DN100"})]
        nearest = [
            {"quote_id": 301, "material": "球墨铸铁闸阀", "why_rejected": "DN不符合"},
        ]
        raw = _llm([{
            "anchor_seq": 10,
            "decision": "missing",
            "confidence": 0.0,
            "quote_ids": [],
            "evidence": "该供应商无此规格报价",
            "nearest_quote_candidates": nearest,
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 0
        assert len(result.dropped) == 1
        d = result.dropped[0]
        assert d["reason"] == "llm_missing"
        assert d["anchor_seq"] == 10
        assert d["nearest_quote_candidates"] == nearest

    def test_missing_without_nearest_candidates_downgrades_to_pending(self):
        """missing with no nearest_quote_candidates must NOT be treated as reliable missing.
        P1: downgrade to pending with 'missing_without_evidence' flag; log as invalid_missing_no_evidence.
        """
        rows = [_row(302, material="截止阀", spec="DN50")]
        anchors = [_anchor(11, name="截止阀", canonical={"valve_type": "截止阀"})]
        raw = _llm([{
            "anchor_seq": 11,
            "decision": "missing",
            "confidence": 0.0,
            "quote_ids": [],
            "evidence": "无报价",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        # Must produce a pending cell (not silently missing) for human review
        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending"
        assert "missing_without_evidence" in cell.flags
        # Must be logged in dropped for audit
        assert any(d["reason"] == "invalid_missing_no_evidence" for d in result.dropped)


# ── Test 4: unknown quote_id → dropped, cell downgrades to pending ───────────

class TestUnknownQuoteId:
    def test_out_of_bounds_quote_id_dropped_cell_pending(self):
        """quote_id not in rows must be dropped; fill downgrades to pending cell."""
        rows = [_row(401, material="闸阀", spec="DN80")]
        anchors = [_anchor(20, name="闸阀", canonical={"valve_type": "闸阀"})]
        raw = _llm([{
            "anchor_seq": 20,
            "decision": "quoted",
            "confidence": 0.7,
            "quote_ids": [9999],   # does not exist in rows
            "evidence": "—",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        # 9999 dropped
        assert any(d.get("reason") == "unknown_quote_id" for d in result.dropped)
        # No valid qids → downgrade to pending placeholder
        assert len(result.cells) == 1
        assert result.cells[0].status == "pending"
        assert result.cells[0].anchor_seq == 20

    def test_mixed_valid_and_invalid_quote_ids(self):
        """Valid qids survive; only invalid ones are dropped."""
        rows = [_row(401, material="闸阀", spec="DN80"), _row(402, material="闸阀", spec="DN80")]
        anchors = [_anchor(20, name="闸阀", canonical={"valve_type": "闸阀"})]
        raw = _llm([{
            "anchor_seq": 20,
            "decision": "quoted",
            "confidence": 0.85,
            "quote_ids": [401, 9999],   # 9999 invalid
            "evidence": "—",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert any(d.get("reason") == "unknown_quote_id" for d in result.dropped)
        assert len(result.cells) == 1
        assert result.cells[0].status == "quoted"
        assert result.cells[0].quote_id == 401


# ── Test 5: multiple quote_ids → aggregated cell ─────────────────────────────

class TestAggregatedMultipleQuotes:
    def test_two_quotes_same_anchor_produces_aggregated_cell(self):
        """decision=aggregated with 2 valid quote_ids → aggregated cell."""
        rows = [
            _row(501, material="截止阀", spec="DN50", unit_price=120.0, qty=5.0),
            _row(502, material="截止阀", spec="DN50", unit_price=110.0, qty=3.0),
        ]
        anchors = [_anchor(30, name="截止阀", canonical={"valve_type": "截止阀", "dn": "DN50"})]
        raw = _llm([{
            "anchor_seq": 30,
            "decision": "aggregated",
            "confidence": 0.9,
            "quote_ids": [501, 502],
            "evidence": "两行均为DN50截止阀，分批交货",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "aggregated"
        assert cell.anchor_seq == 30
        assert set(cell.aggregated_quote_ids) == {501, 502}
        # agg_total = 120×5 + 110×3 = 600 + 330 = 930
        assert cell.agg_total == pytest.approx(930.0)
        assert cell.agg_qty == pytest.approx(8.0)

    def test_single_quote_id_decision_quoted(self):
        """decision=quoted with one valid quote_id → quoted (not aggregated)."""
        rows = [_row(501, material="截止阀", spec="DN50", unit_price=120.0, qty=5.0)]
        anchors = [_anchor(30, name="截止阀", canonical={"valve_type": "截止阀", "dn": "DN50"})]
        raw = _llm([{
            "anchor_seq": 30,
            "decision": "quoted",
            "confidence": 0.9,
            "quote_ids": [501],
            "evidence": "单行DN50截止阀",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        assert result.cells[0].status == "quoted"


# ── Test 6: canonical conflict without OCR correction → pending ───────────────

class TestCanonicalConflictDowngrade:
    def test_valve_type_conflict_downgrades_to_pending(self):
        """Explicit valve_type conflict without OCR correction → pending + canonical_conflict flag."""
        rows = [_row(601, material="截止阀", spec="DN100",
                     canonical={"valve_type": "截止阀", "dn": "DN100"})]
        anchors = [_anchor(40, name="闸阀 DN100",
                           canonical={"valve_type": "闸阀", "dn": "DN100"})]
        raw = _llm([{
            "anchor_seq": 40,
            "decision": "quoted",
            "confidence": 0.6,
            "quote_ids": [601],
            "evidence": "DN100匹配",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending"
        assert "canonical_conflict" in cell.flags
        assert cell.anchor_seq == 40

    def test_no_conflict_when_row_canonical_empty(self):
        """Empty row canonical → wildcard, no conflict even with anchor canonical set."""
        rows = [_row(602, material="给排水闸阀", spec="DN100", canonical={})]
        anchors = [_anchor(41, name="闸阀 DN100",
                           canonical={"valve_type": "闸阀", "dn": "DN100"})]
        raw = _llm([{
            "anchor_seq": 41,
            "decision": "quoted",
            "confidence": 0.75,
            "quote_ids": [602],
            "evidence": "DN100一致",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        assert result.cells[0].status == "quoted"
        assert "canonical_conflict" not in result.cells[0].flags


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_duplicate_anchor_seq_second_dropped(self):
        """Two fills for the same anchor_seq → second is dropped."""
        rows = [_row(701, material="闸阀"), _row(702, material="闸阀")]
        anchors = [_anchor(50, name="闸阀", canonical={})]
        raw = _llm([
            {"anchor_seq": 50, "decision": "quoted", "quote_ids": [701], "confidence": 0.9, "evidence": ""},
            {"anchor_seq": 50, "decision": "quoted", "quote_ids": [702], "confidence": 0.8, "evidence": ""},
        ])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        assert result.cells[0].quote_id == 701
        assert any(d.get("reason") == "duplicate_anchor_seq" for d in result.dropped)

    def test_unknown_anchor_seq_is_dropped(self):
        """Fill referencing a non-existent anchor_seq is dropped without crashing."""
        rows = [_row(801, material="闸阀")]
        anchors = [_anchor(60, name="闸阀")]
        raw = _llm([{
            "anchor_seq": 9999,   # not in anchors
            "decision": "quoted",
            "quote_ids": [801],
            "confidence": 0.8,
            "evidence": "",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 0
        assert any(d.get("reason") == "unknown_anchor_seq" for d in result.dropped)

    def test_empty_fills_list_returns_all_residue(self):
        """Empty LLM output → all rows are residue, no cells."""
        rows = [_row(901, material="闸阀"), _row(902, material="截止阀")]
        anchors = [_anchor(70, name="闸阀")]
        raw = _llm([])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 0
        assert set(result.residue_quote_ids) == {901, 902}

    def test_pending_fill_does_not_consume_quote(self):
        """Pending fill does not go into consumed, so a later quoted fill can use the same qid."""
        rows = [_row(1001, material="减压阀", spec="DN50")]
        anchors = [
            _anchor(80, name="减压阀 DN50", canonical={}),
            _anchor(81, name="减压阀 DN50 备用", canonical={}),
        ]
        raw = _llm([
            {"anchor_seq": 80, "decision": "pending", "quote_ids": [1001], "confidence": 0.5, "evidence": ""},
            {"anchor_seq": 81, "decision": "quoted",  "quote_ids": [1001], "confidence": 0.8, "evidence": ""},
        ])

        result = validate_anchor_fill(raw, anchors, rows)

        statuses = {c.anchor_seq: c.status for c in result.cells}
        assert statuses[80] == "pending"
        assert statuses[81] == "quoted"
        # qid 1001 is referenced by anchor 80 (pending) AND consumed by anchor 81 (quoted)
        # → not in residue either way
        assert 1001 not in result.residue_quote_ids


# ── P1: pending quote excluded from residue ───────────────────────────────────

class TestPendingQuoteNotInResidue:
    def test_pending_only_fill_excludes_qid_from_residue(self):
        """A quote referenced only by a pending fill must NOT appear in residue (P1 fix).
        Before the fix, consumed only tracked quoted/aggregated, so pending qids leaked into residue.
        """
        rows = [_row(2001, material="蝶阀", spec="DN200"), _row(2002, material="闸阀", spec="DN50")]
        anchors = [_anchor(90, name="蝶阀", canonical={"valve_type": "蝶阀"})]
        raw = _llm([{
            "anchor_seq": 90,
            "decision": "pending",
            "quote_ids": [2001],
            "confidence": 0.55,
            "evidence": "规格需人工确认",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        assert result.cells[0].status == "pending"
        assert result.cells[0].quote_id == 2001
        # 2001 referenced by the pending fill — must NOT be in residue
        assert 2001 not in result.residue_quote_ids
        # 2002 has no fill at all — must be in residue
        assert 2002 in result.residue_quote_ids

    def test_excluded_fill_excludes_qid_from_residue(self):
        """Excluded fills also reference their qid; should not appear in residue."""
        rows = [_row(2003, material="截止阀", spec="DN32")]
        anchors = [_anchor(91, name="截止阀", canonical={})]
        raw = _llm([{
            "anchor_seq": 91,
            "decision": "excluded",
            "quote_ids": [2003],
            "confidence": 0.9,
            "evidence": "规格不符，主动排除",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert result.cells[0].status == "excluded"
        assert 2003 not in result.residue_quote_ids


# ── P1: missing without evidence → pending ───────────────────────────────────

class TestMissingEvidenceGate:
    def test_missing_with_evidence_produces_no_cell(self):
        """decision=missing WITH nearest_quote_candidates → no cell (reliable missing)."""
        rows = [_row(3001, material="减压阀", spec="DN50")]
        anchors = [_anchor(100, name="可调式减压阀", canonical={"valve_type": "减压阀"})]
        raw = _llm([{
            "anchor_seq": 100,
            "decision": "missing",
            "quote_ids": [],
            "confidence": 0.1,
            "evidence": "无对应报价",
            "nearest_quote_candidates": [{"quote_id": 3001, "why_rejected": "类型不符"}],
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 0
        assert result.dropped[0]["reason"] == "llm_missing"
        assert result.dropped[0]["nearest_quote_candidates"] != []

    def test_missing_without_evidence_downgrade_creates_pending(self):
        """decision=missing WITHOUT nearest_quote_candidates → pending cell (P1 gate)."""
        rows = [_row(3002, material="减压阀", spec="DN65")]
        anchors = [_anchor(101, name="可调式减压阀 DN65", canonical={})]
        raw = _llm([{
            "anchor_seq": 101,
            "decision": "missing",
            "quote_ids": [],
            "confidence": 0.0,
            "evidence": "—",
            "nearest_quote_candidates": [],
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        assert result.cells[0].status == "pending"
        assert "missing_without_evidence" in result.cells[0].flags
        assert any(d["reason"] == "invalid_missing_no_evidence" for d in result.dropped)
        # Row 3002 is NOT referenced (missing fill had no quote_ids) → still in residue
        assert 3002 in result.residue_quote_ids


# ── P0: attach_nearest_hints anchor_vecs dimension guard ────────────────────

class TestAttachNearestHintsDimensionGuard:
    def test_mismatched_anchor_vecs_are_ignored_and_reembedded(self, monkeypatch):
        """P0: if anchor_vecs has different length than anchors, re-embed instead of using wrong vectors.

        Simulates the analysis.py bug where full-90-anchor vectors were passed
        to an AC pass operating on a 2-anchor gap subset.
        """
        embed_calls: list[list[str]] = []

        def _mock_embed(client, texts):
            embed_calls.append(texts)
            # Return unit vectors (dim=2) for each text
            return [[1.0, 0.0]] * len(texts)

        def _mock_embed_anchor_vecs(anchors, client=None):
            embed_calls.append([f"anchor:{getattr(a, 'name', '')}" for a in anchors])
            return [[0.0, 1.0]] * len(anchors)

        monkeypatch.setattr("apps.api.services.alignment.anchor_match._embed", _mock_embed)
        monkeypatch.setattr("apps.api.services.alignment.anchor_match.embed_anchor_vecs", _mock_embed_anchor_vecs)

        rows = [_row(4001, material="闸阀 DN50"), _row(4002, material="截止阀 DN80")]
        anchors = [
            _anchor(1, name="闸阀 DN50", canonical={}),
            _anchor(2, name="截止阀 DN80", canonical={}),
        ]

        # Pass anchor_vecs with wrong length (90 != 2)
        wrong_vecs = [[float(i), float(i)] for i in range(90)]

        result = attach_nearest_hints(anchors, rows, client=None, k=3, anchor_vecs=wrong_vecs)

        # Should have called embed_anchor_vecs to re-embed the 2-anchor subset
        anchor_embed_calls = [c for c in embed_calls if any("anchor:" in t for t in c)]
        assert anchor_embed_calls, "anchor_vecs mismatch must trigger re-embedding of the subset"
        # Candidates should be returned for both anchors
        assert 1 in result
        assert 2 in result

    def test_correct_anchor_vecs_length_is_accepted(self, monkeypatch):
        """If anchor_vecs matches len(anchors), it must be used as-is (no re-embed)."""
        embed_calls: list[list[str]] = []

        def _mock_embed(client, texts):
            embed_calls.append(texts)
            return [[1.0, 0.0]] * len(texts)

        def _mock_embed_anchor_vecs(anchors, client=None):
            embed_calls.append(["anchor_re_embed"])
            return [[0.0, 1.0]] * len(anchors)

        monkeypatch.setattr("apps.api.services.alignment.anchor_match._embed", _mock_embed)
        monkeypatch.setattr("apps.api.services.alignment.anchor_match.embed_anchor_vecs", _mock_embed_anchor_vecs)

        rows = [_row(4003, material="闸阀")]
        anchors = [_anchor(10, name="闸阀", canonical={})]
        correct_vecs = [[0.5, 0.5]]  # length matches len(anchors) = 1

        attach_nearest_hints(anchors, rows, client=None, k=3, anchor_vecs=correct_vecs)

        # embed_anchor_vecs must NOT have been called (correct vecs were used directly)
        re_embed_called = any("anchor_re_embed" in t for c in embed_calls for t in c)
        assert not re_embed_called, "Correct anchor_vecs must not trigger re-embedding"


# ── OCR correction gate: plausibility rules ───────────────────────────────────

class TestOcrCorrectionPlausibilityGate:
    """Verify that the g2 gate only bypasses for OCR corrections that are type-family-plausible."""

    def test_implausible_flow_test_to_gate_valve_still_denied(self):
        """流量测试接口控制阀门 + correction.to='闸阀' must NOT be quoted to 闸阀 anchor.

        Even if LLM provides an ocr_correction, the from→to crosses product classes
        (流量测试 ≠ parent of 闸阀), so the gate must still fire.
        """
        rows = [_row(5001, material="流量测试接口控制阀门", spec="DN25",
                     canonical={},
                     normalized_material="流量测试接口控制阀门")]
        anchors = [_anchor(1, name="闸阀 DN25",
                           canonical={"valve_type": "闸阀", "dn": "DN25"})]
        raw = _llm([{
            "anchor_seq": 1,
            "decision": "quoted",
            "confidence": 0.78,
            "quote_ids": [5001],
            "evidence": "DN25一致",
            "ocr_correction": {"from": "流量测试接口控制阀门", "to": "闸阀"},
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending", f"Expected pending, got {cell.status} flags={cell.flags}"
        assert any("valve_type_conflict" in f for f in cell.flags)
        assert "ocr_corrected_verified" not in cell.flags

    def test_implausible_vacuum_breaker_to_pressure_reducer_denied(self):
        """真空破坏器 + correction.to='减压阀组' must NOT be quoted to 减压阀组 anchor.

        真空破坏器 is not a parent class of 减压阀组 → gate fires.
        """
        rows = [_row(5002, material="不锈钢真空破坏器", spec="DN20",
                     canonical={},
                     normalized_material="不锈钢真空破坏器")]
        anchors = [_anchor(2, name="减压阀组 DN20",
                           canonical={"valve_type": "减压阀组", "dn": "DN20"})]
        raw = _llm([{
            "anchor_seq": 2,
            "decision": "quoted",
            "confidence": 0.61,
            "quote_ids": [5002],
            "evidence": "DN20一致",
            "ocr_correction": {"from": "不锈钢真空破坏器", "to": "减压阀组"},
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending", f"Expected pending, got {cell.status} flags={cell.flags}"
        assert any("valve_type_conflict" in f for f in cell.flags)

    def test_plausible_ocr_no_explicit_from_uses_raw_text(self):
        """When ocr_correction.from is absent, the raw quote text is used as effective_from.

        橡胶海止回阀 (raw) + correction.to='橡胶瓣止回阀', no explicit from field.
        '止回阀' is a parent of '橡胶瓣止回阀' → bypass allowed.
        """
        rows = [_row(5003, material="橡胶海止回阀", spec="DN65",
                     canonical={},
                     normalized_material="橡胶海止回阀")]
        anchors = [_anchor(3, name="橡胶瓣止回阀 DN65",
                           canonical={"valve_type": "橡胶瓣止回阀", "dn": "DN65"})]
        raw = _llm([{
            "anchor_seq": 3,
            "decision": "quoted",
            "confidence": 0.88,
            "quote_ids": [5003],
            "evidence": "橡胶海为橡胶瓣OCR形近字",
            "ocr_correction": {"to": "橡胶瓣止回阀"},  # no explicit 'from'
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "quoted", f"Expected quoted, got {cell.status} flags={cell.flags}"
        assert "ocr_corrected_verified" in cell.flags

    def test_dn_mismatch_blocks_even_with_valid_type_correction(self):
        """DN mismatch is caught by the initial canonical gate regardless of OCR correction."""
        rows = [_row(5004, material="橡胶海止回阀", spec="DN65",
                     canonical={"dn": "DN65"},
                     normalized_material="橡胶海止回阀")]
        anchors = [_anchor(4, name="橡胶瓣止回阀 DN50",
                           canonical={"valve_type": "橡胶瓣止回阀", "dn": "DN50"})]
        raw = _llm([{
            "anchor_seq": 4,
            "decision": "quoted",
            "confidence": 0.8,
            "quote_ids": [5004],
            "evidence": "类型匹配，DN有误差",
            "ocr_correction": {"from": "橡胶海止回阀", "to": "橡胶瓣止回阀"},
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        # DN65 ≠ DN50 → canonical_conflict from initial gate
        assert cell.status == "pending"
        assert "canonical_conflict" in cell.flags


# ── Test: 减压阀族 family compatibility (P0 deterministic-gate fix) ───────────

class TestValveFamilyCompatibility:
    def test_jianyafa_zu_vs_jianyafa_is_quoted(self):
        """小阻力可调式减压阀组 锚点 + 减压阀 报价 (DN/PN 一致) → quoted, 不被 g/g2 拦截.

        Before the fix: 减压阀组 ≠ 减压阀 → canonical_match_score 0.0 → canonical_conflict,
        and the g2 re-extract gate also fired valve_type_conflict.  Both must now pass.
        """
        rows = [_row(7001, material="小阻力可调式减压阀", spec="DN20 PN16",
                     canonical={"valve_type": "减压阀", "dn": "DN20", "pn": "PN16"})]
        anchors = [_anchor(70, name="小阻力可调式减压阀组", spec="DN20",
                           canonical={"valve_type": "减压阀组", "dn": "DN20", "pn": "PN16"})]
        raw = _llm([{
            "anchor_seq": 70,
            "decision": "quoted",
            "confidence": 0.89,
            "quote_ids": [7001],
            "evidence": "减压阀组与减压阀同族，DN20/PN16一致",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "quoted", f"got {cell.status} flags={cell.flags}"
        assert "canonical_conflict" not in cell.flags
        assert "valve_type_conflict" not in " ".join(cell.flags)

    def test_true_subtype_check_valve_still_blocked(self):
        """橡胶瓣止回阀 锚点 + 旋启式止回阀 报价 (无OCR纠错) → 仍 pending (真子型冲突)."""
        rows = [_row(7002, material="旋启式止回阀", spec="DN50",
                     canonical={"valve_type": "止回阀", "dn": "DN50"})]
        anchors = [_anchor(28, name="橡胶瓣止回阀", spec="DN50",
                           canonical={"valve_type": "橡胶瓣止回阀", "dn": "DN50", "pn": "PN16"})]
        raw = _llm([{
            "anchor_seq": 28,
            "decision": "quoted",
            "confidence": 0.82,
            "quote_ids": [7002],
            "evidence": "旋启式止回阀常规为橡胶瓣密封",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending", f"got {cell.status} flags={cell.flags}"
        assert ("canonical_conflict" in cell.flags
                or "valve_type_conflict" in " ".join(cell.flags))

    def test_non_valve_still_blocked(self):
        """闸阀 锚点 + 流量测试接口控制阀门 报价 → 仍 pending (跨类非阀)."""
        rows = [_row(7003, material="流量测试接口控制阀门", spec="DN25 1.6Mpa",
                     canonical={"valve_type": "流量测试", "dn": "DN25", "pn": "PN16"})]
        anchors = [_anchor(46, name="闸阀", spec="DN25",
                           canonical={"valve_type": "闸阀", "dn": "DN25", "pn": "PN16"})]
        raw = _llm([{
            "anchor_seq": 46,
            "decision": "quoted",
            "confidence": 0.78,
            "quote_ids": [7003],
            "evidence": "DN25一致",
        }])

        result = validate_anchor_fill(raw, anchors, rows)

        assert len(result.cells) == 1
        cell = result.cells[0]
        assert cell.status == "pending", f"got {cell.status} flags={cell.flags}"
        assert ("canonical_conflict" in cell.flags
                or "valve_type_conflict" in " ".join(cell.flags))
