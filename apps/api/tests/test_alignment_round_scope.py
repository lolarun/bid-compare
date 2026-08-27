"""docs/design/42 §4.1 (P2) — round-scoped match wipe-and-rebuild.

Locks the fix for the latent bug design/42 §4.1 recorded: `import_and_match`'s
wipe-and-rebuild used to be scoped only to (project_id, category), so running
match for round 2 silently deleted round 1's `BidAlignmentGroup` rows — the
round a decision may already have been made on. `round_id`, when passed,
narrows that scope to (project_id, category, round_id) instead.
"""
from __future__ import annotations

import uuid

from apps.api.models.bid_alignment import BidAlignmentGroup
from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.project import Project
from apps.api.routes.quotes import BatchConfirmRequest
from apps.api.services.alignment.anchor_match import import_and_match
from apps.api.services.submission.quote_confirmation_service import confirm_batch
from apps.api.services.tender import quote_round_service as svc
from apps.api.services.tender.tender_list import TenderAnchor

# 两个锚点、DN 不同（100/50），命中顺序直连的"区分度"判据——_evidence_verdict
# 的随机一致率检查要求取值不能全同，单锚点/单行永远过不了这道门（n=1 时
# _chance_agreement 恒为 1.0），必须两行起。不依赖 embedding——测试环境的
# dashscope embedding 调用会因账户欠费直接 400（见 HANDOFF）。
ANCHORS = [
    TenderAnchor(seq=1, name="闸阀DN100", spec="Z45X-16Q", unit="个", qty=10, anchor_uid="anchor-1"),
    TenderAnchor(seq=2, name="闸阀DN50", spec="Z45X-16Q", unit="个", qty=5, anchor_uid="anchor-2"),
]
ITEMS = [
    {"material": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10,
     "unit_price": 100.0, "total_price": 1000.0, "category": "阀门"},
    {"material": "闸阀DN50", "spec": "Z45X-16Q", "unit": "个", "qty": 5,
     "unit_price": 80.0, "total_price": 400.0, "category": "阀门"},
]


def _proj(db, code: str) -> Project:
    p = Project(name=f"round-scope-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def _confirm_one(db, project_id: int, category: str, round_id: int, supplier_name: str) -> int:
    j = ExtractionJob(
        id=uuid.uuid4().hex, type="quote", status="done",
        filename=f"{supplier_name}.png",
        result={"items": ITEMS},
    )
    db.add(j)
    db.flush()
    db.commit()
    body = BatchConfirmRequest(
        job_id=j.id, supplier_name=supplier_name, project_id=project_id,
        category=category, round_id=round_id,
    )
    out = confirm_batch(db, body)
    return out["submission_id"]


def test_matching_round_2_does_not_delete_round_1s_groups(db_session):
    proj = _proj(db_session, "S1")
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm_one(db_session, proj.id, "阀门", r1.id, "供应商A")

    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub1], anchors=ANCHORS, round_id=r1.id,
    )
    round1_groups = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == proj.id,
        BidAlignmentGroup.category == "阀门",
        BidAlignmentGroup.round_id == r1.id,
    ).all()
    assert len(round1_groups) == 2  # 2 anchors → 2 groups
    round1_ids = {g.id for g in round1_groups}

    # Open round 2, confirm a different supplier's quote into it, match round 2.
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm_one(db_session, proj.id, "阀门", r2.id, "供应商B")

    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub2], anchors=ANCHORS, round_id=r2.id,
    )

    # THE FIX: round 1's groups must still exist, untouched, after round 2 matched.
    still_there = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.id.in_(round1_ids)
    ).all()
    assert len(still_there) == 2, "round 2 的 match 删掉了 round 1 的对齐组——回归了"

    round2_groups = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == proj.id,
        BidAlignmentGroup.category == "阀门",
        BidAlignmentGroup.round_id == r2.id,
    ).all()
    assert len(round2_groups) == 2
    assert round1_ids.isdisjoint({g.id for g in round2_groups})


def test_rematching_the_same_round_is_idempotent(db_session):
    """Re-running match for round 1 wipes only round 1's own prior groups,
    not a duplicate accumulation — the point of the wipe-and-rebuild at all."""
    proj = _proj(db_session, "S2")
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm_one(db_session, proj.id, "阀门", r1.id, "供应商A")

    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub1], anchors=ANCHORS, round_id=r1.id,
    )
    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub1], anchors=ANCHORS, round_id=r1.id,
    )

    groups = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == proj.id,
        BidAlignmentGroup.round_id == r1.id,
    ).all()
    assert len(groups) == 2  # not 4 — the second call wiped the first's groups, not appended


def test_new_group_carries_round_id_and_anchor_uid(db_session):
    proj = _proj(db_session, "S3")
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm_one(db_session, proj.id, "阀门", r1.id, "供应商A")

    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub1], anchors=ANCHORS, round_id=r1.id,
    )

    group = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == proj.id, BidAlignmentGroup.round_id == r1.id,
        BidAlignmentGroup.anchor_seq == "1",
    ).one()
    assert group.round_id == r1.id
    assert group.anchor_uid == "anchor-1"


def test_round_id_none_preserves_legacy_unscoped_wipe(db_session):
    """A caller that never passes round_id (e.g. preview_service.py, which
    runs inside a rolled-back sandbox transaction) keeps exactly the old
    behavior: wipe scoped to (project, category) only, ignoring round_id."""
    proj = _proj(db_session, "S4")
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm_one(db_session, proj.id, "阀门", r1.id, "供应商A")

    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub1], anchors=ANCHORS, round_id=r1.id,
    )
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm_one(db_session, proj.id, "阀门", r2.id, "供应商B")

    # round_id=None (the default) — legacy scope, wipes everything in
    # (project, category) regardless of round.
    import_and_match(
        db_session, None, proj.id, "阀门",
        submission_ids=[sub2], anchors=ANCHORS, round_id=None,
    )

    groups = db_session.query(BidAlignmentGroup).filter(
        BidAlignmentGroup.project_id == proj.id, BidAlignmentGroup.category == "阀门",
    ).all()
    assert len(groups) == 2  # round 1's groups gone — legacy scope ignores round_id
    assert all(g.round_id is None for g in groups)  # new groups created with no round attribution
