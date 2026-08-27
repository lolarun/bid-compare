"""docs/design/42 P1 — anchor_uid survives a procurement-list revision.

Locks the content-match carry-over rule in
tender_session_service._assign_anchor_uids (docs/design/42 §9's safe
default): a row keeps its anchor_uid only when (name, spec) matches a row in
the immediately-previous version; anything else — new row, or a row whose
name AND spec both changed — gets a fresh uid rather than risk a wrong
carry-over fabricating a trend figure.
"""
from __future__ import annotations

from apps.api.models.project import Project
from apps.api.services.tender import tender_session_service as svc
from apps.api.services.tender.tender_list import rebuild_anchors


def _proj(db, code="AU1"):
    p = Project(name=f"anchor-uid-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def test_first_version_assigns_fresh_uids(db_session):
    proj = _proj(db_session)
    anchors = [
        {"seq": "1", "name": "DN100 闸阀", "spec": "Z45X-16Q"},
        {"seq": "2", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
    ]
    s = svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", anchors, "tester")
    db_session.commit()

    uids = [a["anchor_uid"] for a in s.anchors_json]
    assert all(uids)
    assert len(set(uids)) == 2  # distinct


def test_unchanged_row_keeps_its_uid_across_a_revision(db_session):
    proj = _proj(db_session)
    v1 = [
        {"seq": "1", "name": "DN100 闸阀", "spec": "Z45X-16Q"},
        {"seq": "2", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
    ]
    s1 = svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", v1, "tester")
    db_session.commit()
    uid_a = s1.anchors_json[0]["anchor_uid"]
    uid_b = s1.anchors_json[1]["anchor_uid"]

    # v2: same two rows, PLUS a new one inserted at the front — proves the
    # match is content-based, not position-based (seq shifts for both rows).
    v2 = [
        {"seq": "1", "name": "DN80 球阀", "spec": "Q41F-16Q"},
        {"seq": "2", "name": "DN100 闸阀", "spec": "Z45X-16Q"},
        {"seq": "3", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
    ]
    s2 = svc.save_session(db_session, proj.id, "阀门", "v2.xlsx", v2, "tester")
    db_session.commit()

    by_name = {a["name"]: a for a in s2.anchors_json}
    assert by_name["DN100 闸阀"]["anchor_uid"] == uid_a
    assert by_name["DN50 闸阀"]["anchor_uid"] == uid_b
    assert by_name["DN80 球阀"]["anchor_uid"] not in (uid_a, uid_b)


def test_ambiguous_edit_gets_a_new_uid_not_a_wrong_carryover(db_session):
    """Name AND spec both changed on the same seq — safe default is "new
    row", never a guessed carry-over (docs/design/42 §9)."""
    proj = _proj(db_session)
    v1 = [{"seq": "1", "name": "DN50 闸阀", "spec": "Z45X-16Q"}]
    s1 = svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", v1, "tester")
    db_session.commit()
    uid_1 = s1.anchors_json[0]["anchor_uid"]

    v2 = [{"seq": "1", "name": "DN50 蝶阀", "spec": "D71X-16Q"}]
    s2 = svc.save_session(db_session, proj.id, "阀门", "v2.xlsx", v2, "tester")
    db_session.commit()

    assert s2.anchors_json[0]["anchor_uid"] != uid_1


def test_duplicate_name_spec_rows_each_get_their_own_carryover(db_session):
    """Two identical (name, spec) rows in both versions must not collapse
    onto the same previous uid — first match wins, one-to-one."""
    proj = _proj(db_session)
    v1 = [
        {"seq": "1", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
        {"seq": "2", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
    ]
    s1 = svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", v1, "tester")
    db_session.commit()
    uids_v1 = {a["anchor_uid"] for a in s1.anchors_json}
    assert len(uids_v1) == 2

    v2 = [
        {"seq": "1", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
        {"seq": "2", "name": "DN50 闸阀", "spec": "Z45X-16Q"},
    ]
    s2 = svc.save_session(db_session, proj.id, "阀门", "v2.xlsx", v2, "tester")
    db_session.commit()
    uids_v2 = {a["anchor_uid"] for a in s2.anchors_json}

    assert uids_v2 == uids_v1


def test_rebuild_anchors_carries_anchor_uid_through(db_session):
    proj = _proj(db_session)
    v1 = [{"seq": "1", "name": "DN50 闸阀", "spec": "Z45X-16Q"}]
    s1 = svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", v1, "tester")
    db_session.commit()

    rebuilt = rebuild_anchors(s1)
    assert len(rebuilt) == 1
    assert rebuilt[0].anchor_uid == s1.anchors_json[0]["anchor_uid"]
    assert rebuilt[0].anchor_uid != ""
