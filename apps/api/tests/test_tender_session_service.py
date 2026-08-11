"""Contract tests for TenderSessionService (P1-1 first slice).

Locks the behavior extracted from routes/analysis.py inline queries so the
service stays the single authoritative definition of current / confirmed /
version / deactivation.
"""
from __future__ import annotations

from apps.api.models.tender_list_session import TenderListSession
from apps.api.services import tender_session_service as svc


def _mk(db, *, project_id, category, version, is_current, status="confirmed"):
    s = TenderListSession(
        project_id=project_id,
        category=category,
        file_name=f"v{version}.xlsx",
        anchors_total=0,
        anchors_json=[],
        version=version,
        is_current=is_current,
        status=status,
    )
    db.add(s)
    db.commit()
    return s


def test_save_session_supersedes_and_bumps_version(db_session):
    old = _mk(db_session, project_id=1, category="阀门", version=1, is_current=True)
    new = svc.save_session(
        db_session, 1, "阀门", "v2.xlsx", [{"name": "闸阀"}], "tester"
    )
    db_session.commit()

    db_session.refresh(old)
    assert old.is_current is False
    assert old.superseded_at is not None
    assert new.version == 2
    assert new.is_current is True
    assert new.status == "confirmed"
    assert new.anchors_total == 1


def test_save_session_first_version_is_one(db_session):
    s = svc.save_session(db_session, 9, "桥架", "f.xlsx", [], None)
    db_session.commit()
    assert s.version == 1
    assert s.is_current is True


def test_get_current_confirmed_session_requires_both_gates(db_session):
    # is_current but NOT confirmed → must not be returned by the confirmed gate
    _mk(db_session, project_id=2, category="阀门", version=1,
        is_current=True, status="preview")
    assert svc.get_current_confirmed_session(db_session, 2, "阀门") is None

    confirmed = _mk(db_session, project_id=3, category="阀门", version=1,
                    is_current=True, status="confirmed")
    got = svc.get_current_confirmed_session(db_session, 3, "阀门")
    assert got is not None and got.id == confirmed.id


def test_get_current_session_any_status_ignores_status(db_session):
    """get_current_session_any_status returns the current row regardless of confirmation."""
    preview = _mk(db_session, project_id=4, category="阀门", version=1,
                  is_current=True, status="preview")
    got = svc.get_current_session_any_status(db_session, "阀门", project_id=4)
    assert got is not None and got.id == preview.id


def test_list_current_sessions_scopes_to_project_and_current(db_session):
    _mk(db_session, project_id=5, category="阀门", version=1, is_current=True)
    _mk(db_session, project_id=5, category="桥架", version=1, is_current=True)
    _mk(db_session, project_id=5, category="阀门", version=0, is_current=False)
    _mk(db_session, project_id=6, category="阀门", version=1, is_current=True)

    rows = svc.list_current_sessions(db_session, 5)
    cats = sorted(r.category for r in rows)
    assert cats == ["桥架", "阀门"]


def test_list_versions_newest_first(db_session):
    _mk(db_session, project_id=7, category="阀门", version=1, is_current=False)
    _mk(db_session, project_id=7, category="阀门", version=2, is_current=True)
    rows = svc.list_versions(db_session, "阀门", project_id=7)
    assert [r.version for r in rows] == [2, 1]


def test_deactivate_current_marks_not_current(db_session):
    s = _mk(db_session, project_id=8, category="阀门", version=1, is_current=True)
    n = svc.deactivate_current(db_session, "阀门", project_id=8)
    assert n == 1
    db_session.refresh(s)
    assert s.is_current is False
    assert s.superseded_at is not None


# ── Second-slice helper tests ────────────────────────────────────────────────


def test_get_any_current_confirmed_session_returns_most_recent(db_session):
    _mk(db_session, project_id=10, category="阀门", version=1, is_current=False, status="confirmed")
    s2 = _mk(db_session, project_id=10, category="桥架", version=1, is_current=True, status="confirmed")
    got = svc.get_any_current_confirmed_session(db_session, 10)
    assert got is not None and got.id == s2.id


def test_get_any_current_confirmed_session_ignores_unconfirmed(db_session):
    _mk(db_session, project_id=11, category="阀门", version=1, is_current=True, status="preview")
    assert svc.get_any_current_confirmed_session(db_session, 11) is None


def test_get_session_for_fill_by_explicit_id(db_session):
    s = _mk(db_session, project_id=12, category="阀门", version=1, is_current=True, status="preview")
    got = svc.get_session_for_fill(db_session, 12, "阀门", tls_id=s.id)
    assert got is not None and got.id == s.id


def test_get_session_for_fill_fallback_to_current(db_session):
    s = _mk(db_session, project_id=13, category="阀门", version=1, is_current=True, status="preview")
    got = svc.get_session_for_fill(db_session, 13, "阀门", tls_id=None)
    assert got is not None and got.id == s.id


def test_record_submission_scope_writes_ids(db_session):
    s = _mk(db_session, project_id=14, category="阀门", version=1, is_current=True)
    svc.record_submission_scope(db_session, s.id, sub_ids=[1, 3, 5], supplier_ids=[10, 20])
    db_session.refresh(s)
    assert s.used_submission_ids == [1, 3, 5]
    assert s.confirmed_supplier_ids == [10, 20]


def test_record_submission_scope_none_sub_ids_preserves_existing(db_session):
    s = _mk(db_session, project_id=15, category="阀门", version=1, is_current=True)
    # First call sets both
    svc.record_submission_scope(db_session, s.id, sub_ids=[1, 2], supplier_ids=[10])
    db_session.refresh(s)
    # Second call with sub_ids=None should NOT clear used_submission_ids
    svc.record_submission_scope(db_session, s.id, sub_ids=None, supplier_ids=[30])
    db_session.refresh(s)
    assert s.used_submission_ids == [1, 2]  # unchanged
    assert s.confirmed_supplier_ids == [30]  # updated
