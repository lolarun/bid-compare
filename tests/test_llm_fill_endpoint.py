"""Phase 5: /tender-list/llm-fill endpoint orchestration (embedding + LLM mocked).

Exercises the async fan-out, baseline-from-topk, response assembly and the
replace→finalization-invalidation closure end-to-end without network.
"""
import asyncio
import json

import pytest

import apps.api.services.alignment.anchor_match as am
from apps.api.models import Material, Supplier, Project, Quote
from apps.api.models.tender_list_session import TenderListSession
from apps.api.models.alignment_finalization import AlignmentFinalization
from apps.api.routes.analysis import tender_list_llm_fill, _LlmFillBody


# ─── fake embedding + LLM ─────────────────────────────────────────────────────

_VECS = {
    "球阀": [1.0, 0.0, 0.0],
    "闸阀": [0.0, 1.0, 0.0],
}


def _vec_for(text: str):
    for k, v in _VECS.items():
        if k in text:
            return v
    return [0.0, 0.0, 1.0]


class _FakeUsage:
    total_tokens = 10


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, content):
        self._content = content
        self.chat = type("Ch", (), {"completions": self})()

    def create(self, **kw):
        return _FakeResp(self._content)


@pytest.fixture
def fill_env(db_session, monkeypatch):
    # deterministic embeddings
    monkeypatch.setattr(am, "_embed", lambda client, texts: [_vec_for(t) for t in texts])

    proj = Project(name="EndpointProj", status="进行中")
    sup_a = Supplier(name="供A", short_name="A", categories=["阀门"])
    sup_b = Supplier(name="供B", short_name="B", categories=["阀门"])
    db_session.add_all([proj, sup_a, sup_b])
    db_session.flush()

    m1 = Material(material_code="V1", standard_name="球阀DN50", profession="暖通",
                  category="阀门", unit="个", extended_attrs={"canonical": {"valve_type": "球阀", "dn": "DN50"}})
    db_session.add(m1)
    db_session.flush()

    qa = Quote(material_id=m1.id, supplier_id=sup_a.id, project_id=proj.id,
               unit_price=100.0, quantity=10.0, total_price=1000.0)
    qb = Quote(material_id=m1.id, supplier_id=sup_b.id, project_id=proj.id,
               unit_price=110.0, quantity=10.0, total_price=1100.0)
    db_session.add_all([qa, qb])
    db_session.flush()

    session = TenderListSession(
        project_id=proj.id, category="阀门", file_name="t.xlsx",
        anchors_total=2, version=1, is_current=True, status="confirmed",
        anchors_json=[
            {"seq": 1, "name": "球阀", "spec": "DN50",
             "canonical": {"valve_type": "球阀", "dn": "DN50"}},
            {"seq": 2, "name": "闸阀", "spec": "DN80",
             "canonical": {"valve_type": "闸阀", "dn": "DN80"}},
        ],
    )
    db_session.add(session)
    db_session.commit()

    return {"db": db_session, "proj": proj, "sup_a": sup_a, "sup_b": sup_b,
            "qa": qa, "qb": qb, "session": session, "monkeypatch": monkeypatch}


def _run(coro):
    return asyncio.run(coro)


def test_llm_fill_endpoint_produces_comparable_and_baseline(fill_env, monkeypatch):
    s = fill_env
    # LLM maps each supplier's quote to anchor 1 as quoted
    def fake_fill_client():
        # assignment references the supplier's actual quote ids; build per-call below
        return _FakeClient(json.dumps({"assignments": [
            {"quote_id": s["qa"].id, "anchor_seq": 1, "status": "quoted", "confidence": 0.8},
            {"quote_id": s["qb"].id, "anchor_seq": 1, "status": "quoted", "confidence": 0.8},
        ]}))

    # one client serves both workers; each worker only sees its own rows so the
    # cross-supplier assignment ids are harmlessly ignored (unknown_quote_id drop)
    client = fake_fill_client()
    monkeypatch.setattr(am, "_embed_client", lambda: client)

    body = _LlmFillBody(project_id=s["proj"].id, category="阀门",
                        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
                        tender_list_session_id=s["session"].id)
    resp = _run(tender_list_llm_fill(body, s["db"]))

    assert resp["anchors_total"] == 2
    # both suppliers land quote on anchor 1 → comparable
    assert resp["comparable_2plus"] == 1, resp
    # embedding baseline computed from topk (both quotes embed to anchor 1)
    assert resp["comparable_2plus_embedding_baseline"] == 1
    assert len(resp["per_supplier_fill"]) == 2
    assert resp["finalization_invalidated"] is False  # none existed


def test_llm_fill_invalidates_finalization(fill_env, monkeypatch):
    s = fill_env
    # pre-existing finalized snapshot
    fin = AlignmentFinalization(project_id=s["proj"].id, category="阀门",
                                status="finalized", group_ids_json=[1, 2, 3])
    s["db"].add(fin)
    s["db"].commit()

    client = _FakeClient(json.dumps({"assignments": [
        {"quote_id": s["qa"].id, "anchor_seq": 1, "status": "quoted", "confidence": 0.8},
        {"quote_id": s["qb"].id, "anchor_seq": 1, "status": "quoted", "confidence": 0.8},
    ]}))
    monkeypatch.setattr(am, "_embed_client", lambda: client)

    body = _LlmFillBody(project_id=s["proj"].id, category="阀门",
                        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
                        tender_list_session_id=s["session"].id)
    resp = _run(tender_list_llm_fill(body, s["db"]))

    assert resp["finalization_invalidated"] is True
    s["db"].expire_all()
    assert s["db"].get(AlignmentFinalization, fin.id).status == "superseded"
