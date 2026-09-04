"""Phase 5: LLM-fill persistence + orchestration helpers.

2026-08-28: moved here from the now-retired root `tests/` (test-root
consolidation) and renamed from `test_llm_fill_persistence.py` — that name
collided with an unrelated, already-existing file of the same name in this
directory (soft-delete/supersede semantics, safety gate, force_partial;
see `test_llm_fill_persistence.py`). Same feature area, disjoint coverage,
kept as two files rather than merged.

Tests the deterministic persistence layer (no real LLM / no network):
  - _persist_llm_fill writes one group per anchor_seq + one item per cell
  - aggregated cell persists ONE align item carrying agg_total/agg_qty
  - re-run replaces prior [llm-fill] groups (idempotent), leaves other groups
  - build_anchor_matrix consumes the written items unchanged
"""
import pytest
from sqlalchemy import select

from apps.api.models import Material, Project, Quote, Supplier
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.tender_list_session import TenderListSession
from apps.api.routes.analysis import _persist_llm_fill
from apps.api.services.supplier.supplier_fill_llm import FillCell, SupplierFillResult


class _Anchor:
    def __init__(self, seq, name, spec="", pressure="", unit="个", qty=None, canonical=None):
        self.seq = seq
        self.name = name
        self.spec = spec
        self.pressure = pressure
        self.unit = unit
        self.qty = qty
        self.canonical = canonical or {}

    def material_text(self):
        return str(self.canonical.get("material") or "")


@pytest.fixture
def fill_setup(db_session):
    proj = Project(name="LlmFillProj", status="进行中")
    sup_a = Supplier(name="供A", short_name="A", categories=["阀门"])
    sup_b = Supplier(name="供B", short_name="B", categories=["阀门"])
    db_session.add_all([proj, sup_a, sup_b])
    db_session.flush()
    session = TenderListSession(project_id=proj.id, category="阀门")
    db_session.add(session)
    db_session.flush()

    mats = []
    for code, name in [("V1", "球阀DN50"), ("V2", "球阀DN50b"), ("V3", "闸阀DN80")]:
        m = Material(material_code=code, standard_name=name, profession="暖通",
                     category="阀门", unit="个")
        db_session.add(m)
        mats.append(m)
    db_session.flush()

    # supplier A: two quotes for anchor 1 (to be aggregated), one for anchor 2
    qa1 = Quote(material_id=mats[0].id, supplier_id=sup_a.id, project_id=proj.id,
                unit_price=100.0, quantity=10.0, total_price=1000.0)
    qa2 = Quote(material_id=mats[1].id, supplier_id=sup_a.id, project_id=proj.id,
                unit_price=120.0, quantity=5.0, total_price=600.0)
    # supplier B: one quote for anchor 1
    qb1 = Quote(material_id=mats[0].id, supplier_id=sup_b.id, project_id=proj.id,
                unit_price=110.0, quantity=10.0, total_price=1100.0)
    db_session.add_all([qa1, qa2, qb1])
    db_session.commit()

    anchors = [
        _Anchor(seq=1, name="球阀", spec="DN50", canonical={"valve_type": "球阀", "dn": "DN50"}),
        _Anchor(seq=2, name="闸阀", spec="DN80", canonical={"valve_type": "闸阀", "dn": "DN80"}),
    ]
    return {
        "db": db_session, "proj": proj, "sup_a": sup_a, "sup_b": sup_b,
        "anchors": anchors, "seq_to_anchor": {int(a.seq): a for a in anchors}, "session_id": session.id,
        "qa1": qa1, "qa2": qa2, "qb1": qb1,
    }


def test_persist_writes_group_per_anchor_and_item_per_cell(fill_setup):
    s = fill_setup
    db = s["db"]
    valid_sids = {s["sup_a"].id, s["sup_b"].id}

    # supplier A aggregates qa1+qa2 on anchor 1; supplier B quotes qb1 on anchor 1
    res_a = SupplierFillResult(supplier_id=s["sup_a"].id, cells=[
        FillCell(anchor_seq=1, supplier_id=s["sup_a"].id, action="align", status="aggregated",
                 quote_id=s["qa1"].id, unit_price=100.0, qty=10.0, total_price=1000.0,
                 agg_total=1600.0, agg_qty=15.0,
                 aggregated_quote_ids=[s["qa1"].id, s["qa2"].id], confidence=0.9),
    ])
    res_b = SupplierFillResult(supplier_id=s["sup_b"].id, cells=[
        FillCell(anchor_seq=1, supplier_id=s["sup_b"].id, action="align", status="quoted",
                 quote_id=s["qb1"].id, unit_price=110.0, qty=10.0, total_price=1100.0,
                 confidence=0.88),
    ])
    _persist_llm_fill(db, s["proj"].id, "阀门", s["session_id"], [res_a, res_b], s["seq_to_anchor"], valid_sids)
    db.commit()

    groups = db.scalars(select(BidAlignmentGroup).where(
        BidAlignmentGroup.project_id == s["proj"].id,
        BidAlignmentGroup.reason.like("[llm-fill]%"),
    )).all()
    assert len(groups) == 1  # only anchor 1 had cells
    g = groups[0]
    assert g.anchor_seq == "1"
    assert g.status == "confirmed"
    assert g.tender_list_session_id == s["session_id"]

    items = db.scalars(select(BidAlignmentItem).where(BidAlignmentItem.group_id == g.id)).all()
    assert len(items) == 2  # one per supplier
    agg_item = next(i for i in items if i.supplier_id == s["sup_a"].id)
    assert agg_item.action == "align"
    assert agg_item.agg_total == 1600.0
    assert agg_item.agg_qty == 15.0
    assert "aggregated=" in agg_item.spec_note
    assert "cos=" in agg_item.spec_note  # matrix confidence parser needs this


def test_rerun_replaces_only_llm_fill_groups(fill_setup):
    s = fill_setup
    db = s["db"]
    valid_sids = {s["sup_a"].id, s["sup_b"].id}

    # Pre-existing NON-llm-fill group (e.g. from embedding) must survive
    other = BidAlignmentGroup(project_id=s["proj"].id, category="阀门",
                              suggested_name="其他", reason="招标清单锚点 #5",
                              status="confirmed", anchor_seq="5")
    db.add(other)
    db.commit()
    other_id = other.id

    res = SupplierFillResult(supplier_id=s["sup_a"].id, cells=[
        FillCell(anchor_seq=2, supplier_id=s["sup_a"].id, action="align", status="quoted",
                 quote_id=s["qa1"].id, unit_price=100.0, qty=10.0, total_price=1000.0,
                 confidence=0.9),
    ])
    # First run
    _persist_llm_fill(db, s["proj"].id, "阀门", s["session_id"], [res], s["seq_to_anchor"], valid_sids)
    db.commit()
    first = db.scalars(select(BidAlignmentGroup).where(
        BidAlignmentGroup.reason.like("[llm-fill]%"),
        BidAlignmentGroup.status == "confirmed")).all()
    assert len(first) == 1

    # Second run (idempotent replace)
    _persist_llm_fill(db, s["proj"].id, "阀门", s["session_id"], [res], s["seq_to_anchor"], valid_sids)
    db.commit()
    second = db.scalars(select(BidAlignmentGroup).where(
        BidAlignmentGroup.reason.like("[llm-fill]%"),
        BidAlignmentGroup.status == "confirmed")).all()
    assert len(second) == 1, "re-run must replace, not duplicate"

    # The non-llm-fill group is untouched
    assert db.get(BidAlignmentGroup, other_id) is not None


def test_persisted_items_render_in_anchor_matrix(fill_setup):
    """build_anchor_matrix must consume llm-fill items unchanged."""
    from apps.api.services.matrix.bid_matrix import build_anchor_matrix

    s = fill_setup
    db = s["db"]
    valid_sids = {s["sup_a"].id, s["sup_b"].id}

    res_a = SupplierFillResult(supplier_id=s["sup_a"].id, cells=[
        FillCell(anchor_seq=1, supplier_id=s["sup_a"].id, action="align", status="quoted",
                 quote_id=s["qa1"].id, unit_price=100.0, qty=10.0, total_price=1000.0,
                 confidence=0.9),
    ])
    res_b = SupplierFillResult(supplier_id=s["sup_b"].id, cells=[
        FillCell(anchor_seq=1, supplier_id=s["sup_b"].id, action="align", status="quoted",
                 quote_id=s["qb1"].id, unit_price=110.0, qty=10.0, total_price=1100.0,
                 confidence=0.88),
    ])
    _persist_llm_fill(db, s["proj"].id, "阀门", s["session_id"], [res_a, res_b], s["seq_to_anchor"], valid_sids)
    db.commit()

    matrix = build_anchor_matrix(
            db=db, anchors=s["anchors"], tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id, category="阀门",
    )
    assert len(matrix["rows"]) == 2  # all anchors present
    row1 = next(r for r in matrix["rows"] if r["anchor_seq"] == "1")
    quoted = [c for c in row1["suppliers"] if c.get("price") is not None]
    assert len(quoted) == 2  # both suppliers quoted anchor 1 → comparable
