"""Regression test for build_anchor_matrix confirmed-alignment-group branch (P0-1).

Exercises the code path where BidAlignmentGroup.status='confirmed' and
BidAlignmentItem.action='align' links a BidQuoteLine — the original P0-1
bug site (the legacy path queried the unaligned quote table instead of
reading confirmed groups; this ensures the confirmed-group branch is covered).
"""
from __future__ import annotations

import uuid

import pytest

from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.models.extraction_job import ExtractionJob
from apps.api.services.matrix.bid_matrix import build_anchor_matrix
from apps.api.services.tender.tender_list import TenderAnchor

CATEGORY = "阀门"


def _job(db) -> str:
    j = ExtractionJob(id=uuid.uuid4().hex, type="quote", status="done", filename="bid.pdf")
    db.add(j)
    db.flush()
    return j.id


def _sub(db, project_id: int):
    s = BidSubmission(
        job_id=_job(db),
        project_id=project_id,
        supplier_raw_name="虚构供应商甲",
        batch_id=f"batch-{uuid.uuid4().hex}",
        status="pending",
    )
    db.add(s)
    db.flush()
    return s


def _bql(db, sub_id: int, unit_price: float = 100.0, qty: float = 2.0):
    bql = BidQuoteLine(
        submission_id=sub_id,
        raw_name="闸阀DN50",
        standard_name="闸阀",
        category=CATEGORY,
        spec="DN50",
        unit="个",
        qty=qty,
        unit_price=unit_price,
        total_price=round(unit_price * qty, 2),
    )
    db.add(bql)
    db.flush()
    return bql


def _group(db, project_id: int, anchor_seq: str = "1"):
    g = BidAlignmentGroup(
        project_id=project_id,
        category=CATEGORY,
        anchor_seq=anchor_seq,
        suggested_name="闸阀DN50",
        status="confirmed",
    )
    db.add(g)
    db.flush()
    return g


def _call_matrix(db, project_id: int, sub_id: int, anchor_seq: str = "1"):
    anchor = TenderAnchor(seq=anchor_seq, name="闸阀", spec="DN50", unit="个", qty=2.0)
    return build_anchor_matrix(
        db,
        anchors=[anchor],
        tender_list_session_id=None,
        supplier_ids=[],
        project_id=project_id,
        category=CATEGORY,
        used_submission_ids=[sub_id],
    )


def test_confirmed_group_align_item_produces_quoted_cell(db_session):
    """Confirmed group with action='align' must return cell_status='quoted' with price."""
    sub = _sub(db_session, project_id=1)
    bql = _bql(db_session, sub.id, unit_price=100.0)
    g = _group(db_session, project_id=1)
    db_session.add(BidAlignmentItem(
        group_id=g.id, action="align",
        submission_id=sub.id, bid_quote_line_id=bql.id,
    ))
    db_session.flush()

    result = _call_matrix(db_session, project_id=1, sub_id=sub.id)

    rows = result["rows"]
    assert len(rows) == 1
    cell = rows[0]["suppliers"][0]
    assert cell["cell_status"] == "quoted"
    assert cell["price"] == pytest.approx(100.0)


def test_no_group_produces_missing_cell(db_session):
    """No alignment group for the anchor → cell must be 'missing'."""
    sub = _sub(db_session, project_id=2)

    result = _call_matrix(db_session, project_id=2, sub_id=sub.id, anchor_seq="99")

    assert result["rows"][0]["suppliers"][0]["cell_status"] == "missing"


def test_pending_item_produces_pending_cell(db_session):
    """action='pending' must produce cell_status='pending', not 'quoted'."""
    sub = _sub(db_session, project_id=3)
    bql = _bql(db_session, sub.id, unit_price=500.0)
    g = _group(db_session, project_id=3, anchor_seq="2")
    db_session.add(BidAlignmentItem(
        group_id=g.id, action="pending",
        submission_id=sub.id, bid_quote_line_id=bql.id,
    ))
    db_session.flush()

    anchor = TenderAnchor(seq="2", name="闸阀DN100", spec="DN100", unit="个", qty=2.0)
    result = build_anchor_matrix(
        db_session,
        anchors=[anchor],
        tender_list_session_id=None,
        supplier_ids=[],
        project_id=3,
        category=CATEGORY,
        used_submission_ids=[sub.id],
    )

    assert result["rows"][0]["suppliers"][0]["cell_status"] == "pending"


def test_session_scoped_group_filter(db_session):
    """tender_list_session_id filter: only groups from the given session appear."""
    sub = _sub(db_session, project_id=4)
    bql = _bql(db_session, sub.id, unit_price=200.0)

    # group belongs to session 99 — will be excluded when we pass session_id=88
    g = BidAlignmentGroup(
        project_id=4, category=CATEGORY, anchor_seq="1",
        suggested_name="闸阀DN50", status="confirmed",
        tender_list_session_id=99,
    )
    db_session.add(g)
    db_session.flush()
    db_session.add(BidAlignmentItem(
        group_id=g.id, action="align",
        submission_id=sub.id, bid_quote_line_id=bql.id,
    ))
    db_session.flush()

    anchor = TenderAnchor(seq="1", name="闸阀", spec="DN50", unit="个", qty=2.0)
    result = build_anchor_matrix(
        db_session,
        anchors=[anchor],
        tender_list_session_id=88,   # different session — group filtered out
        supplier_ids=[],
        project_id=4,
        category=CATEGORY,
        used_submission_ids=[sub.id],
    )

    assert result["rows"][0]["suppliers"][0]["cell_status"] == "missing"
