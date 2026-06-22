"""Unit tests for build_anchor_matrix (v2.5 anchor-full-axis matrix).

Verifies:
  1. rows == len(anchors) (all anchors appear even if unmatched)
  2. missing cells exist for unmatched anchors
  3. BidMatrixResult.model_validate(result) succeeds (schema compat)
  4. pending cells do NOT contribute to supplier totals
  5. align cells DO contribute to totals; is_lowest is correct
  6. multi align item: lowest effective price is selected (not highest)
"""
import pytest
from dataclasses import dataclass, field
from typing import Any

from apps.api.models import Material, Supplier, Project, Quote
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.schemas.analysis import BidMatrixResult
from apps.api.services.bid_matrix import build_anchor_matrix, CELL_MISSING, CELL_PENDING, CELL_QUOTED


# ─── Minimal TenderAnchor stub ────────────────────────────────────────────────

@dataclass
class _Anchor:
    """Minimal TenderAnchor-compatible stub for tests."""
    seq: int
    name: str
    spec: str = ""
    model: str = ""
    pressure: str = ""
    materials: dict = field(default_factory=dict)
    unit: str = "个"
    qty: float | None = None
    profession: str = ""
    canonical: dict = field(default_factory=dict)

    def material_text(self) -> str:
        return " ".join(v for v in self.materials.values() if v)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def anchor_setup(db_session):
    """Set up two anchors, two suppliers, one project.

    Anchor 1: has alignment group + align item from supplier A + pending item from supplier B
    Anchor 2: NO alignment group at all (both suppliers missing)
    """
    # NOTE: AnalysisConfig is already seeded by conftest db_session fixture
    proj = Project(name="TestAnchorMatrix", status="进行中")
    db_session.add(proj)

    sup_a = Supplier(name="供应商Alpha", short_name="Alpha", categories=["阀门"])
    sup_b = Supplier(name="供应商Beta",  short_name="Beta",  categories=["阀门"])
    db_session.add_all([sup_a, sup_b])
    db_session.flush()

    mat_a = Material(
        material_code="V-001", standard_name="闸阀 DN50",
        profession="暖通", category="阀门", unit="个",
    )
    mat_b = Material(
        material_code="V-002", standard_name="截止阀 DN25",
        profession="暖通", category="阀门", unit="个",
    )
    db_session.add_all([mat_a, mat_b])
    db_session.flush()

    # Anchor 1 quotes
    qt_a1 = Quote(
        material_id=mat_a.id, supplier_id=sup_a.id,
        project_id=proj.id, unit_price=100.0, quantity=10.0,
    )
    qt_b1 = Quote(
        material_id=mat_b.id, supplier_id=sup_b.id,
        project_id=proj.id, unit_price=120.0, quantity=10.0,
    )
    db_session.add_all([qt_a1, qt_b1])
    db_session.flush()

    # Anchor 1 alignment group
    grp = BidAlignmentGroup(
        project_id=proj.id,
        category="阀门",
        suggested_name="闸阀",
        suggested_spec="DN50",
        confidence=0.85,
        status="confirmed",
        anchor_seq="1",           # links to anchor with seq=1
        tender_list_session_id=99,  # fake session id
    )
    db_session.add(grp)
    db_session.flush()

    # sup_a → align; sup_b → pending
    item_align = BidAlignmentItem(
        group_id=grp.id, quote_id=qt_a1.id,
        supplier_id=sup_a.id, action="align",
        spec_note="cos=0.85",
    )
    item_pending = BidAlignmentItem(
        group_id=grp.id, quote_id=qt_b1.id,
        supplier_id=sup_b.id, action="pending",
        spec_note="cos=0.62",
    )
    db_session.add_all([item_align, item_pending])
    db_session.commit()

    anchors = [
        _Anchor(seq=1, name="闸阀", spec="DN50"),
        _Anchor(seq=2, name="蝶阀", spec="DN100"),  # no group — both missing
    ]

    return {
        "db": db_session,
        "proj": proj,
        "sup_a": sup_a,
        "sup_b": sup_b,
        "anchors": anchors,
        "session_id": 99,
        "qt_a1": qt_a1,
        "qt_b1": qt_b1,
    }


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_anchor_matrix_row_count_equals_anchors(anchor_setup):
    """Every anchor produces exactly one row."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    assert len(result["rows"]) == len(s["anchors"]), (
        f"Expected {len(s['anchors'])} rows, got {len(result['rows'])}"
    )


def test_anchor_matrix_missing_cells_for_unmatched_anchor(anchor_setup):
    """Anchor 2 (no group) → all cells are missing."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    row2 = next(r for r in result["rows"] if r["anchor_seq"] == "2")
    for cell in row2["suppliers"]:
        assert cell["cell_status"] == CELL_MISSING, (
            f"Expected missing for anchor 2, got {cell['cell_status']}"
        )
        assert cell["price"] is None


def test_anchor_matrix_pydantic_schema_validates(anchor_setup):
    """BidMatrixResult.model_validate must not raise (schema compat check)."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    result["anchor_matrix"] = True
    # Should not raise
    validated = BidMatrixResult.model_validate(result)
    assert validated.anchor_matrix is True
    assert len(validated.rows) == 2


def test_pending_cell_excluded_from_totals(anchor_setup):
    """Supplier B has pending item on anchor 1 — must NOT appear in totals."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    totals_by_sid = {t["supplier_id"]: t for t in result["totals"]}
    sup_b_total = totals_by_sid[s["sup_b"].id]

    # Supplier B only has a pending item — quoted_count must be 0
    assert sup_b_total["quoted_count"] == 0, (
        f"pending cell should NOT count in quoted_count, got {sup_b_total['quoted_count']}"
    )
    # And total price must be 0.0 (pending price excluded)
    assert sup_b_total["total"] == 0.0, (
        f"pending cell price should NOT sum into total, got {sup_b_total['total']}"
    )


def test_align_cell_contributes_to_totals(anchor_setup):
    """Supplier A has align item on anchor 1 — must appear in totals."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    totals_by_sid = {t["supplier_id"]: t for t in result["totals"]}
    sup_a_total = totals_by_sid[s["sup_a"].id]
    assert sup_a_total["quoted_count"] == 1
    assert sup_a_total["total"] > 0


def test_pending_cell_has_price_and_item_id(anchor_setup):
    """Pending cell for supplier B on anchor 1 shows reference price + item_id."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    row1 = next(r for r in result["rows"] if r["anchor_seq"] == "1")
    cell_b = next(c for c in row1["suppliers"] if c["supplier_id"] == s["sup_b"].id)

    assert cell_b["cell_status"] == CELL_PENDING
    assert cell_b["price"] == 120.0, "Pending cell should expose reference price (method A)"
    assert cell_b["item_id"] is not None, "Pending cell must carry item_id for inline confirm"
    assert cell_b["confidence"] is not None


def test_align_cell_is_lowest_correct(anchor_setup):
    """Anchor 1 has only supplier A quoted → is_lowest=True for that cell."""
    s = anchor_setup
    result = build_anchor_matrix(
        db=s["db"],
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    row1 = next(r for r in result["rows"] if r["anchor_seq"] == "1")
    cell_a = next(c for c in row1["suppliers"] if c["supplier_id"] == s["sup_a"].id)
    assert cell_a["cell_status"] == CELL_QUOTED
    # Only supplier A has a quoted price, so it's the lowest by default
    assert cell_a["is_lowest"] is True


def test_multi_align_items_lowest_price_selected(anchor_setup):
    """When a supplier has two align items for the same anchor, the lower price is selected."""
    s = anchor_setup
    db = s["db"]

    # Add a second align item for supplier A with a higher price (200 > 100)
    mat_c = Material(
        material_code="V-003", standard_name="闸阀 DN50 备用",
        profession="暖通", category="阀门", unit="个",
    )
    db.add(mat_c)
    db.flush()
    qt_a2 = Quote(
        material_id=mat_c.id, supplier_id=s["sup_a"].id,
        project_id=s["proj"].id, unit_price=200.0, quantity=5.0,
    )
    db.add(qt_a2)
    db.flush()

    # Add second align item to the existing group for anchor_seq=1
    from apps.api.models.bid_alignment import BidAlignmentGroup as BAG
    grp = db.query(BAG).filter(BAG.anchor_seq == "1").first()
    item2 = BidAlignmentItem(
        group_id=grp.id, quote_id=qt_a2.id,
        supplier_id=s["sup_a"].id, action="align",
        spec_note="cos=0.80",
    )
    db.add(item2)
    db.commit()

    result = build_anchor_matrix(
        db=db,
        anchors=s["anchors"],
        tender_list_session_id=s["session_id"],
        supplier_ids=[s["sup_a"].id, s["sup_b"].id],
        project_id=s["proj"].id,
        category="阀门",
    )
    row1 = next(r for r in result["rows"] if r["anchor_seq"] == "1")
    cell_a = next(c for c in row1["suppliers"] if c["supplier_id"] == s["sup_a"].id)

    # Should pick the lower price (100, not 200)
    assert cell_a["price"] == 100.0, (
        f"Should select lowest price among align items, got {cell_a['price']}"
    )
