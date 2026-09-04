"""Contract tests for SubmissionScopeService (P1-1).

Locks the authority rules of resolve_active_submissions():
- submission_ids is authoritative and exclusive (no union with supplier_ids)
- superseded submissions are excluded
- submission_ids priority overrides supplier_ids when both provided
"""
from __future__ import annotations

from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.project import Project
from apps.api.models.supplier import Supplier
from apps.api.services.submission.bid_submission_resolve import (
    resolve_active_submissions,
)


def _job(db, tag: str) -> str:
    import uuid
    j = ExtractionJob(
        id=uuid.uuid4().hex,
        type="quote",
        status="done",
        filename=f"{tag}.pdf",
    )
    db.add(j)
    db.flush()
    return j.id


def _proj(db, code: str):
    p = Project(name=f"proj-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def _sub(db, proj_id, sup_id, status="pending", tag="s"):
    s = BidSubmission(
        job_id=_job(db, tag),
        project_id=proj_id,
        supplier_id=sup_id,
        batch_id=f"batch-{tag}",
        status=status,
    )
    db.add(s)
    db.flush()
    return s


def _bql(db, sub_id, category="阀门"):
    db.add(BidQuoteLine(
        submission_id=sub_id,
        standard_name="闸阀DN50",
        spec="DN50",
        unit="个",
        qty=1,
        unit_price=100,
        category=category,
    ))
    db.flush()


def test_excludes_superseded(db_session):
    """superseded submission must be excluded, same as rejected."""
    proj = _proj(db_session, "SS-001")
    sup = Supplier(name="SS供应商", merge_status="active")
    db_session.add(sup)
    db_session.flush()

    sub = _sub(db_session, proj.id, sup.id, status="superseded", tag="ss")
    _bql(db_session, sub.id)

    result = resolve_active_submissions(db_session, proj.id, "阀门")
    assert sub.id not in result


def test_submission_ids_overrides_supplier_ids(db_session):
    """submission_ids non-empty → use only those, ignore supplier_ids completely."""
    proj = _proj(db_session, "SI-001")
    sup_a = Supplier(name="A供应商", merge_status="active")
    sup_b = Supplier(name="B供应商", merge_status="active")
    db_session.add_all([sup_a, sup_b])
    db_session.flush()

    sub_a = _sub(db_session, proj.id, sup_a.id, tag="sia")
    sub_b = _sub(db_session, proj.id, sup_b.id, tag="sib")
    _bql(db_session, sub_a.id)
    _bql(db_session, sub_b.id)

    # Caller passes sub_a via submission_ids AND sup_b via supplier_ids
    result = resolve_active_submissions(
        db_session, proj.id, "阀门",
        submission_ids=[sub_a.id],
        supplier_ids=[sup_b.id],
    )
    # Only sub_a should be returned; sup_b must NOT appear
    assert sub_a.id in result
    assert sub_b.id not in result


def test_no_union_between_submission_ids_and_supplier_ids(db_session):
    """submission_ids and supplier_ids must NOT be unioned."""
    proj = _proj(db_session, "NU-001")
    sup_a = Supplier(name="A供应商", merge_status="active")
    sup_b = Supplier(name="B供应商", merge_status="active")
    db_session.add_all([sup_a, sup_b])
    db_session.flush()

    sub_a = _sub(db_session, proj.id, sup_a.id, tag="nua")
    sub_b = _sub(db_session, proj.id, sup_b.id, tag="nub")
    _bql(db_session, sub_a.id)
    _bql(db_session, sub_b.id)

    result = resolve_active_submissions(
        db_session, proj.id, "阀门",
        submission_ids=[sub_a.id],
        supplier_ids=[sup_b.id],
    )
    assert len(result) == 1


def test_empty_both_returns_all_active(db_session):
    """Both empty → return all active submissions for the project+category."""
    proj = _proj(db_session, "EA-001")
    for i in range(3):
        sup = Supplier(name=f"EA供应商{i}", merge_status="active")
        db_session.add(sup)
        db_session.flush()
        sub = _sub(db_session, proj.id, sup.id, tag=f"ea{i}")
        _bql(db_session, sub.id)

    result = resolve_active_submissions(db_session, proj.id, "阀门")
    assert len(result) == 3


def test_submission_ids_empty_list_falls_through_to_supplier_ids(db_session):
    """Explicitly empty submission_ids [] is falsy → fall through to supplier_ids."""
    proj = _proj(db_session, "EL-001")
    sup = Supplier(name="EL供应商", merge_status="active")
    db_session.add(sup)
    db_session.flush()
    sub = _sub(db_session, proj.id, sup.id, tag="el")
    _bql(db_session, sub.id)

    result = resolve_active_submissions(
        db_session, proj.id, "阀门",
        submission_ids=[],       # falsy → ignored
        supplier_ids=[sup.id],
    )
    assert sub.id in result
