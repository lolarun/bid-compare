"""Contract tests for QuoteRound (docs/design/42 P0).

Locks the invariants quote_round_service enforces:
- at most one `open` round per (project_id, category)
- opening/reopening a round closes whatever round was open before it
- is_final_basis is explicit-only, and setting it clears it on siblings
- category scoping is independent within the same project
"""
from __future__ import annotations

import pytest

from apps.api.models.project import Project
from apps.api.models.quote_round import QuoteRound
from apps.api.services.tender import quote_round_service as svc


def _proj(db, code="R1"):
    p = Project(name=f"round-test-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def test_get_or_open_round_auto_creates_round_1(db_session):
    proj = _proj(db_session)
    r = svc.get_or_open_round(db_session, proj.id, "阀门")
    db_session.commit()

    assert r.seq == 1
    assert r.name == "第1轮"
    assert r.status == "open"
    assert r.is_final_basis is False

    # Calling again returns the SAME round, not a new one.
    r2 = svc.get_or_open_round(db_session, proj.id, "阀门")
    assert r2.id == r.id


def test_create_round_closes_the_previously_open_round(db_session):
    proj = _proj(db_session)
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    assert r1.status == "open"

    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    db_session.refresh(r1)
    assert r1.status == "closed"
    assert r1.closed_at is not None
    assert r2.status == "open"
    assert r2.seq == 2


def test_category_scoping_is_independent(db_session):
    proj = _proj(db_session)
    valve_r1 = svc.create_round(db_session, proj.id, "阀门", name="阀门第一轮")
    cable_r1 = svc.create_round(db_session, proj.id, "电缆", name="电缆第一轮")
    db_session.commit()

    # Opening a round in "电缆" must not touch "阀门"'s open round.
    db_session.refresh(valve_r1)
    assert valve_r1.status == "open"
    assert cable_r1.status == "open"
    assert valve_r1.seq == 1
    assert cable_r1.seq == 1


def test_rename_round(db_session):
    proj = _proj(db_session)
    r = svc.create_round(db_session, proj.id, "阀门")
    db_session.commit()

    out = svc.rename_round(db_session, r.id, "招标前摸底")
    assert out.name == "招标前摸底"


def test_reopen_closes_sibling_open_round(db_session):
    proj = _proj(db_session)
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    svc.reopen_round(db_session, r1.id)

    db_session.refresh(r1)
    db_session.refresh(r2)
    assert r1.status == "open"
    assert r1.closed_at is None
    assert r2.status == "closed"


def test_set_final_basis_is_exclusive_within_scope(db_session):
    proj = _proj(db_session)
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    svc.set_final_basis(db_session, r1.id, True)
    db_session.refresh(r1)
    assert r1.is_final_basis is True
    assert svc.get_final_basis_round(db_session, proj.id, "阀门").id == r1.id

    # Flagging r2 as basis must clear r1's flag — at most one at a time.
    svc.set_final_basis(db_session, r2.id, True)
    db_session.refresh(r1)
    db_session.refresh(r2)
    assert r1.is_final_basis is False
    assert r2.is_final_basis is True
    assert svc.get_final_basis_round(db_session, proj.id, "阀门").id == r2.id


def test_no_round_flagged_final_basis_returns_none(db_session):
    """docs/design/42 §8 D3 — no auto-promotion. A project with rounds but no
    explicit basis flag has no official round, full stop."""
    proj = _proj(db_session)
    svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()

    assert svc.get_final_basis_round(db_session, proj.id, "阀门") is None


def test_create_round_rejects_unknown_stage(db_session):
    proj = _proj(db_session)
    with pytest.raises(ValueError):
        svc.create_round(db_session, proj.id, "阀门", stage="not_a_real_stage")


def test_list_rounds_ordered_newest_first(db_session):
    proj = _proj(db_session)
    svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    rounds = svc.list_rounds(db_session, proj.id, "阀门")
    assert [r.seq for r in rounds] == [2, 1]


def test_record_round_scope_writes_its_own_round_not_others(db_session):
    """docs/design/42 §3.1 — the entire reason this exists: a second round's
    scope write must never touch the first round's stored scope."""
    proj = _proj(db_session)
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    svc.record_round_scope(db_session, r1.id, [1, 2], [10], tender_list_session_id=99)
    db_session.refresh(r1)
    assert r1.used_submission_ids == [1, 2]
    assert r1.confirmed_supplier_ids == [10]
    assert r1.tender_list_session_id == 99

    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()
    svc.record_round_scope(db_session, r2.id, [3], [20], tender_list_session_id=99)

    db_session.refresh(r1)
    db_session.refresh(r2)
    assert r1.used_submission_ids == [1, 2]  # untouched by round 2's write
    assert r2.used_submission_ids == [3]


def test_record_round_scope_none_fields_skip_not_clear(db_session):
    proj = _proj(db_session)
    r = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    svc.record_round_scope(db_session, r.id, [1], [10])
    svc.record_round_scope(db_session, r.id, None, None)  # e.g. a caller that only knows one field
    db_session.refresh(r)
    assert r.used_submission_ids == [1]
    assert r.confirmed_supplier_ids == [10]
