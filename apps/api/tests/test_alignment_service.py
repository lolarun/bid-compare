"""Contract tests for AlignmentService (P1-1).

Locks the two safety gates of finalize_alignment():
- force=True requires reason
- pending items block finalization unless force=True
- valve_type_conflict items block finalization unless force=True
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.services.alignment_service import finalize_alignment


def _group(db, project_id, category, status="confirmed"):
    g = BidAlignmentGroup(
        project_id=project_id,
        category=category,
        anchor_seq="1",
        suggested_name="闸阀DN50",
        status=status,
    )
    db.add(g)
    db.flush()
    return g


def _item(db, group_id, action="align", spec_note=""):
    i = BidAlignmentItem(
        group_id=group_id,
        action=action,
        spec_note=spec_note,
    )
    db.add(i)
    db.flush()
    return i


def test_force_without_reason_raises_400(db_session):
    with pytest.raises(HTTPException) as exc_info:
        finalize_alignment(db_session, project_id=1, category="阀门", force=True, reason="")
    assert exc_info.value.status_code == 400


def test_pending_items_block_without_force(db_session):
    g = _group(db_session, project_id=2, category="阀门")
    _item(db_session, g.id, action="pending")

    with pytest.raises(HTTPException) as exc_info:
        finalize_alignment(db_session, project_id=2, category="阀门", force=False)
    assert exc_info.value.status_code == 409
    assert "pending" in str(exc_info.value.detail)


def test_pending_items_allowed_with_force(db_session):
    g = _group(db_session, project_id=3, category="阀门")
    _item(db_session, g.id, action="pending")

    result = finalize_alignment(
        db_session, project_id=3, category="阀门",
        force=True, reason="测试强制完成"
    )
    assert result.forced is True
    assert result.pending_at_finalize == 1


def test_valve_type_conflict_blocks_without_force(db_session):
    g = _group(db_session, project_id=4, category="阀门")
    _item(db_session, g.id, action="align", spec_note="valve_type_conflict:butterfly_vs_gate")

    with pytest.raises(HTTPException) as exc_info:
        finalize_alignment(db_session, project_id=4, category="阀门", force=False)
    assert exc_info.value.status_code == 409  # gate blocks: conflict items found


def test_clean_state_finalizes_successfully(db_session):
    g = _group(db_session, project_id=5, category="阀门")
    _item(db_session, g.id, action="align")

    result = finalize_alignment(db_session, project_id=5, category="阀门")
    assert result.group_ids_count == 1
    assert result.pending_at_finalize == 0
    assert result.forced is False
    assert result.id is not None


def test_finalization_captures_group_id_snapshot(db_session):
    g1 = _group(db_session, project_id=6, category="阀门")
    g2 = _group(db_session, project_id=6, category="阀门")

    result = finalize_alignment(db_session, project_id=6, category="阀门")
    assert result.group_ids_count == 2
