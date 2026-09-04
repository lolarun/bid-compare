"""Contract tests for BidExportService (best-practice review finding C1).

Regression coverage for two defects found during the review:
  1. Export used to read via get_current_session (any status) instead of
     get_current_confirmed_session — an unconfirmed/preview session could be
     exported as if it were the official comparison result.
  2. Export hand-rolled its own TenderAnchor rebuild instead of calling the
     authoritative tender_list.rebuild_anchors — dropping brand/remark/
     source_ref, so exported "brand requirement" columns were always empty
     even though the page (which uses rebuild_anchors) showed values.
"""
from __future__ import annotations

import pytest

from apps.api.core.errors import ConflictError
from apps.api.models.tender_list_session import TenderListSession
from apps.api.services.matrix import bid_export_service


def _mk_session(db, *, project_id, category, status, anchors_json=None):
    s = TenderListSession(
        project_id=project_id,
        category=category,
        file_name="v1.xlsx",
        anchors_total=len(anchors_json or []),
        anchors_json=anchors_json or [],
        version=1,
        is_current=True,
        status=status,
    )
    db.add(s)
    db.commit()
    return s


def test_export_rejects_unconfirmed_session(db_session, monkeypatch):
    """A preview (never-confirmed) session must not be exportable — 409, not silently used.

    409 (not 400): 评审 E1 —— "无已确认采购清单" 与 build_anchor_review_matrix/
    analysis.py:208 是同一语义，统一到 ConflictError(409)。
    """
    _mk_session(
        db_session, project_id=1, category="阀门", status="preview",
        anchors_json=[{"seq": 1, "name": "闸阀"}],
    )

    called = {"build_anchor_matrix": False}
    monkeypatch.setattr(
        bid_export_service, "build_anchor_matrix",
        lambda *a, **kw: called.__setitem__("build_anchor_matrix", True) or {},
    )

    with pytest.raises(ConflictError) as exc:
        bid_export_service.get_bid_matrix_for_export(db_session, 1, "阀门", [])

    assert exc.value.status_code == 409
    assert called["build_anchor_matrix"] is False


def test_export_uses_authoritative_rebuild_anchors(db_session, monkeypatch):
    """Confirmed session's anchors must carry brand/remark/source_ref (rebuild_anchors), not a lossy hand copy."""
    anchors_json = [{
        "seq": 1,
        "name": "闸阀",
        "spec": "Z41H-16C",
        "model": "",
        "pressure": "1.6MPa",
        "materials": {},
        "unit": "台",
        "qty": 2,
        "brand": "示例品牌",
        "profession": "给排水",
        "remark": "备注示例",
        "source_ref": {"page": 3},
    }]
    _mk_session(
        db_session, project_id=2, category="阀门", status="confirmed",
        anchors_json=anchors_json,
    )

    captured = {}

    def _fake_build_anchor_matrix(db, *, anchors, **kw):
        captured["anchors"] = anchors
        return {"cells": []}

    monkeypatch.setattr(bid_export_service, "build_anchor_matrix", _fake_build_anchor_matrix)

    bid_export_service.get_bid_matrix_for_export(db_session, 2, "阀门", [])

    anchors = captured["anchors"]
    assert len(anchors) == 1
    assert anchors[0].brand == "示例品牌"
    assert anchors[0].remark == "备注示例"
    assert anchors[0].source_ref == {"page": 3}
