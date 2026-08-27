"""docs/design/42 §6 — round-over-round price trend.

Exercises `compute_round_trend` end to end (real `TenderListSession`, real
`QuoteRound`s, real `import_and_match` → `build_anchor_matrix`), mirroring
what `routes/analysis.py::tender_list_match` wires together, rather than
calling the FastAPI route function directly (which needs `UploadFile`
dependency injection outside the test client). Uses the same 2-anchor,
distinguishing-DN fixture as test_alignment_round_scope.py so sequential
direct-connect matching passes without a real embedding call (the dashscope
account is in arrears in this environment — see HANDOFF).
"""
from __future__ import annotations

import uuid

from apps.api.models.extraction_job import ExtractionJob
from apps.api.models.project import Project
from apps.api.routes.quotes import BatchConfirmRequest
from apps.api.services.alignment.anchor_match import import_and_match
from apps.api.services.matrix.round_trend import compute_round_trend
from apps.api.services.submission.quote_confirmation_service import confirm_batch
from apps.api.services.tender import quote_round_service as svc
from apps.api.services.tender import tender_session_service as tls_svc
from apps.api.services.tender.tender_list import rebuild_anchors

ANCHORS_JSON = [
    {"seq": "1", "name": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10},
    {"seq": "2", "name": "闸阀DN50", "spec": "Z45X-16Q", "unit": "个", "qty": 5},
]


def _proj(db, code: str) -> Project:
    p = Project(name=f"round-trend-{code}", code=code)
    db.add(p)
    db.flush()
    return p


def _confirm(db, project_id: int, category: str, round_id: int, supplier: str,
             price100: float, price50: float) -> int:
    j = ExtractionJob(
        id=uuid.uuid4().hex, type="quote", status="done", filename=f"{supplier}.png",
        result={"items": [
            {"material": "闸阀DN100", "spec": "Z45X-16Q", "unit": "个", "qty": 10,
             "unit_price": price100, "total_price": price100 * 10, "category": category},
            {"material": "闸阀DN50", "spec": "Z45X-16Q", "unit": "个", "qty": 5,
             "unit_price": price50, "total_price": price50 * 5, "category": category},
        ]},
    )
    db.add(j)
    db.flush()
    db.commit()
    out = confirm_batch(db, BatchConfirmRequest(
        job_id=j.id, supplier_name=supplier, project_id=project_id,
        category=category, round_id=round_id,
    ))
    return out["submission_id"]


def _run_round(db, project_id: int, category: str, round_id: int, session_id: int,
               sub_ids: list[int]) -> None:
    """Mirror routes/analysis.py::tender_list_match's wiring for one round."""
    session = tls_svc.get_current_session_any_status(db, category, project_id=project_id)
    anchors = rebuild_anchors(session)
    import_and_match(
        db, None, project_id, category,
        submission_ids=sub_ids, anchors=anchors,
        tender_list_session_id=session_id, round_id=round_id,
    )
    svc.record_round_scope(
        db, round_id, sorted(sub_ids), None, tender_list_session_id=session_id,
    )


def test_two_rounds_same_supplier_cheaper_second_round(db_session):
    proj = _proj(db_session, "T1")
    session = tls_svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub1 = _confirm(db_session, proj.id, "阀门", r1.id, "供应商A", price100=100.0, price50=80.0)
    _run_round(db_session, proj.id, "阀门", r1.id, session.id, [sub1])

    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()
    sub2 = _confirm(db_session, proj.id, "阀门", r2.id, "供应商A", price100=90.0, price50=80.0)
    _run_round(db_session, proj.id, "阀门", r2.id, session.id, [sub2])

    result = compute_round_trend(db_session, proj.id, "阀门")

    assert result.round_ids == [r1.id, r2.id]
    assert result.skipped_rounds == []

    dn100_rows = sorted(
        (r for r in result.rows if r.anchor_uid == session.anchors_json[0]["anchor_uid"]),
        key=lambda r: r.round_seq,
    )
    assert [r.unit_price for r in dn100_rows] == [100.0, 90.0]
    r2_point = dn100_rows[1]
    assert r2_point.comparable_to_prev is True
    assert r2_point.round_over_round_discount_pct == 10.0  # (100-90)/100 * 100

    dn50_rows = sorted(
        (r for r in result.rows if r.anchor_uid == session.anchors_json[1]["anchor_uid"]),
        key=lambda r: r.round_seq,
    )
    # Unchanged price → 0% discount, not "not comparable".
    assert dn50_rows[1].comparable_to_prev is True
    assert dn50_rows[1].round_over_round_discount_pct == 0.0


def test_supplier_absent_from_round_2_is_not_zero_filled(db_session):
    """R4: a supplier who didn't participate in round 2 must not appear as a
    round-2 point at all — never a fabricated 0 or a fabricated -100%."""
    proj = _proj(db_session, "T2")
    session = tls_svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    r1 = svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()
    sub_a = _confirm(db_session, proj.id, "阀门", r1.id, "供应商A", price100=100.0, price50=80.0)
    sub_b = _confirm(db_session, proj.id, "阀门", r1.id, "供应商B", price100=110.0, price50=85.0)
    _run_round(db_session, proj.id, "阀门", r1.id, session.id, [sub_a, sub_b])

    # Round 2: only 供应商A returns.
    r2 = svc.create_round(db_session, proj.id, "阀门", name="第二轮")
    db_session.commit()
    sub_a2 = _confirm(db_session, proj.id, "阀门", r2.id, "供应商A", price100=95.0, price50=80.0)
    _run_round(db_session, proj.id, "阀门", r2.id, session.id, [sub_a2])

    result = compute_round_trend(db_session, proj.id, "阀门")

    sup_b_rows = [r for r in result.rows if r.supplier_name == "供应商B"]
    assert all(r.round_seq == 1 for r in sup_b_rows), (
        "供应商B 缺席第二轮，不该有第二轮的数据点——不管是不是 0"
    )

    sup_b_summaries = [s for s in result.suppliers if s.supplier_name == "供应商B"]
    assert all(s.round_seq == 1 for s in sup_b_summaries)


def test_round_with_no_match_ever_run_is_skipped_not_guessed(db_session):
    proj = _proj(db_session, "T3")
    tls_svc.save_session(db_session, proj.id, "阀门", "v1.xlsx", ANCHORS_JSON, "tester")
    db_session.commit()

    # Round exists (e.g. opened for an upcoming collection) but match never ran.
    svc.create_round(db_session, proj.id, "阀门", name="第一轮")
    db_session.commit()

    result = compute_round_trend(db_session, proj.id, "阀门")

    assert result.rows == []
    assert len(result.skipped_rounds) == 1
    assert "对齐范围" in result.skipped_rounds[0]["reason"]
