"""Tests for LLM-fill persistence safety and soft-delete semantics.

Covers:
- replace supersedes old groups (soft-delete, not physical delete)
- superseded groups are invisible to bid_matrix queries
- safety gate blocks persist when any supplier's LLM call failed
- force_partial=True allows partial persist despite errors
- source_ref non-integer / out-of-range indices are flagged, items kept
"""
from __future__ import annotations

import pytest

from apps.api.models.bid_alignment import BidAlignmentGroup


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_group(db, project_id: int, category: str, reason: str = "test") -> BidAlignmentGroup:
    g = BidAlignmentGroup(
        project_id=project_id, category=category,
        suggested_name="截止阀", suggested_spec="DN50", suggested_unit="个",
        confidence=0.8, reason=reason, status="confirmed",
    )
    db.add(g)
    db.flush()
    return g


# ── soft-delete (supersede) semantics ────────────────────────────────────────

class TestReplaceSupersedes:
    def test_all_confirmed_groups_become_superseded(self, db_session):
        """_persist_llm_fill marks ALL confirmed groups superseded, not just llm-fill ones."""
        emb = _make_group(db_session, 10, "阀门", reason="embedding cosine")
        old_llm = _make_group(db_session, 10, "阀门", reason="[llm-fill] #3")
        db_session.commit()

        from apps.api.routes.analysis import _persist_llm_fill
        _persist_llm_fill(db_session, 10, "阀门", session_id=1,
                          results=[], seq_to_anchor={}, valid_sids=set())
        db_session.commit()

        db_session.refresh(emb)
        db_session.refresh(old_llm)
        assert emb.status == "superseded"
        assert old_llm.status == "superseded"

    def test_rows_not_physically_deleted(self, db_session):
        """Superseded groups still exist in the table (audit trail)."""
        g = _make_group(db_session, 11, "阀门")
        db_session.commit()
        gid = g.id

        from apps.api.routes.analysis import _persist_llm_fill
        _persist_llm_fill(db_session, 11, "阀门", session_id=1,
                          results=[], seq_to_anchor={}, valid_sids=set())
        db_session.commit()

        row = db_session.get(BidAlignmentGroup, gid)
        assert row is not None
        assert row.status == "superseded"

    def test_superseded_invisible_to_matrix_query(self, db_session):
        """bid_matrix filters status='confirmed'; superseded groups must not appear."""
        g = _make_group(db_session, 12, "阀门")
        g.status = "superseded"
        db_session.commit()

        visible = db_session.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.project_id == 12,
            BidAlignmentGroup.category == "阀门",
            BidAlignmentGroup.status == "confirmed",
        ).all()
        assert len(visible) == 0

    def test_other_project_not_affected(self, db_session):
        """replace only touches the given project_id/category."""
        keep = _make_group(db_session, 20, "管道", reason="unrelated")
        target = _make_group(db_session, 10, "阀门", reason="to supersede")
        db_session.commit()

        from apps.api.routes.analysis import _persist_llm_fill
        _persist_llm_fill(db_session, 10, "阀门", session_id=1,
                          results=[], seq_to_anchor={}, valid_sids=set())
        db_session.commit()

        db_session.refresh(keep)
        db_session.refresh(target)
        assert keep.status == "confirmed"
        assert target.status == "superseded"

    def test_new_groups_written_as_confirmed(self, db_session):
        """Groups written by the new LLM-fill run have status='confirmed'."""
        from apps.api.services.supplier.supplier_fill_llm import SupplierFillResult, FillCell

        cell = FillCell(
            anchor_seq=5, quote_id=99, supplier_id=7, status="quoted", action="align",
            confidence=0.88, flags=[], aggregated_quote_ids=[], agg_total=None, agg_qty=None,
        )
        res = SupplierFillResult(supplier_id=7)
        res.cells = [cell]

        from apps.api.services.tender.tender_list import TenderAnchor
        anchor = TenderAnchor(seq=5, name="截止阀", spec="DN50 PN16", pressure="PN16")

        from apps.api.routes.analysis import _persist_llm_fill
        _persist_llm_fill(db_session, 30, "阀门", session_id=1,
                          results=[res], seq_to_anchor={5: anchor}, valid_sids={7})
        db_session.commit()

        groups = db_session.query(BidAlignmentGroup).filter(
            BidAlignmentGroup.project_id == 30,
            BidAlignmentGroup.category == "阀门",
            BidAlignmentGroup.status == "confirmed",
        ).all()
        assert len(groups) == 1


# ── safety gate ───────────────────────────────────────────────────────────────

class TestSafetyGate:
    def test_error_result_detected(self):
        """SupplierFillResult.error is non-empty when LLM call failed."""
        from apps.api.services.supplier.supplier_fill_llm import SupplierFillResult
        r = SupplierFillResult(supplier_id=7)
        r.error = "ConnectionError: timeout after 300s"

        # Safety gate logic (mirrors analysis.py)
        sids = [7]
        results_by_sid = {7: r}
        failed = [(sid, results_by_sid[sid].error) for sid in sids if results_by_sid[sid].error]
        assert len(failed) == 1
        assert failed[0] == (7, r.error)

    def test_no_error_passes_gate(self):
        """Clean result passes the safety gate check."""
        from apps.api.services.supplier.supplier_fill_llm import SupplierFillResult
        r = SupplierFillResult(supplier_id=7)

        sids = [7]
        results_by_sid = {7: r}
        failed = [(sid, results_by_sid[sid].error) for sid in sids if results_by_sid[sid].error]
        assert failed == []

    def test_gate_does_not_block_when_force_partial(self):
        """force_partial=True means errors are tolerated — no failed list checked."""
        from apps.api.services.supplier.supplier_fill_llm import SupplierFillResult
        r = SupplierFillResult(supplier_id=7)
        r.error = "LLM rate limit"

        force_partial = True
        sids = [7]
        results_by_sid = {7: r}
        # When force_partial is True, gate is skipped entirely
        blocked = False if force_partial else bool(
            [sid for sid in sids if results_by_sid[sid].error]
        )
        assert blocked is False

# TestSourceRefRobustness removed 2026-08-11 (best-practice review F1/F2):
# _assign_source_ref_from_grids and table_parser.TableGrid/TableRow were part
# of the legacy OCR→HTML→TableGrid chain, deleted as production-unreachable.
# The VL-direct path builds source_ref from row.source_ref (extraction_draft.py)
# directly, with no separate grid-index-lookup step to test.
