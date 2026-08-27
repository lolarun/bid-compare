"""docs/design/42 P0 — confirm_batch attaches submissions to a QuoteRound.

Locks the wiring in quote_confirmation_service.confirm_batch:
- omitting round_id auto-opens (or reuses) round 1 for (project, category)
- an explicit round_id is honored instead of the auto-opened round
- a second confirm_batch call for a DIFFERENT round attaches to that round,
  without disturbing the first round's submission
"""
from __future__ import annotations

from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.project import Project
from apps.api.models.bid_submission import BidSubmission
from apps.api.routes.quotes import BatchConfirmRequest
from apps.api.services.submission.quote_confirmation_service import confirm_batch
from apps.api.services.tender import quote_round_service as svc

ITEMS = [
    {"material": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10,
     "unit_price": 100.0, "total_price": 1000.0, "category": "阀门"},
]


def _job(db, tag: str) -> str:
    import uuid
    j = ExtractionJob(
        id=uuid.uuid4().hex, type="quote", status="done",
        filename=f"{tag}.png", result={"items": ITEMS},
    )
    db.add(j)
    db.flush()
    return j.id


def _proj(db, code: str) -> Project:
    p = Project(name=f"round-attach-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def test_confirm_batch_auto_opens_round_1_when_omitted(db_session):
    proj = _proj(db_session, "A1")
    job_id = _job(db_session, "s1")
    db_session.commit()

    body = BatchConfirmRequest(
        job_id=job_id, supplier_name="供应商A", project_id=proj.id, category="阀门",
    )
    out = confirm_batch(db_session, body)

    sub = db_session.get(BidSubmission, out["submission_id"])
    assert sub.round_id is not None
    round_ = svc.get_open_round(db_session, proj.id, "阀门")
    assert sub.round_id == round_.id
    assert round_.seq == 1


def test_confirm_batch_honors_explicit_round_id(db_session):
    proj = _proj(db_session, "A2")
    # Two explicit rounds; round 1 gets closed when round 2 opens.
    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    job_id = _job(db_session, "s2")
    db_session.commit()

    # Explicitly target the CLOSED round 1, not the currently-open round 2 —
    # proves round_id isn't silently overridden by "current open round".
    body = BatchConfirmRequest(
        job_id=job_id, supplier_name="供应商B", project_id=proj.id, category="阀门",
        round_id=r1.id,
    )
    out = confirm_batch(db_session, body)

    sub = db_session.get(BidSubmission, out["submission_id"])
    assert sub.round_id == r1.id
    assert sub.round_id != r2.id


def test_two_rounds_attach_independently(db_session):
    proj = _proj(db_session, "A3")
    job1 = _job(db_session, "r1sup")
    db_session.commit()

    body1 = BatchConfirmRequest(
        job_id=job1, supplier_name="供应商A", project_id=proj.id, category="阀门",
    )
    out1 = confirm_batch(db_session, body1)
    round1 = svc.get_open_round(db_session, proj.id, "阀门")

    # Start round 2 explicitly — closes round 1.
    round2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()

    job2 = _job(db_session, "r2sup")
    db_session.commit()
    body2 = BatchConfirmRequest(
        job_id=job2, supplier_name="供应商A", project_id=proj.id, category="阀门",
    )
    out2 = confirm_batch(db_session, body2)

    sub1 = db_session.get(BidSubmission, out1["submission_id"])
    sub2 = db_session.get(BidSubmission, out2["submission_id"])

    assert sub1.round_id == round1.id
    assert sub2.round_id == round2.id
    assert sub1.round_id != sub2.round_id
    # Round 1's submission is untouched by round 2's confirm — no bleed-over.
    assert sub1.round_id == round1.id


def test_confirm_batch_with_no_project_leaves_round_id_null(db_session):
    """No (project, category) to attach to — round_id stays null rather than
    guessing. Matches the pre-round behaviour for project-less submissions."""
    job_id = _job(db_session, "noproj")
    db_session.commit()

    body = BatchConfirmRequest(
        job_id=job_id, supplier_name="陌生供应商", category="阀门",
    )
    out = confirm_batch(db_session, body)

    sub = db_session.get(BidSubmission, out["submission_id"])
    assert sub.round_id is None
