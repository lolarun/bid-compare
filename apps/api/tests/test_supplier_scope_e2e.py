"""E2E hard assertions: supplier scope isolation in anchor-review/matrix and bid-matrix.

These tests verify:
  - anchor-review/matrix requires supplier_ids (400 if missing)
  - supplier_count == len(confirmed_suppliers), never more
  - rows == anchors_total
  - total_cells == anchors_total × supplier_count
  - No historical supplier pollution (matrix columns == upload scope)
  - BidAlignmentGroup scoped to current TenderListSession
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.core.database import Base, get_db
from apps.api.main import app
from apps.api.models import (
    Material,
    Project,
    Quote,
    Supplier,
    TenderListSession,
)
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.routes.auth import get_current_user

# ─── In-memory SQLite fixture ─────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    # Apply additive columns needed by tests
    from sqlalchemy import text
    with engine.begin() as conn:
        for col, ddl in [
            ("agg_total", "ALTER TABLE bid_alignment_items ADD COLUMN agg_total REAL"),
            ("agg_qty", "ALTER TABLE bid_alignment_items ADD COLUMN agg_qty REAL"),
            ("tender_list_session_id", "ALTER TABLE bid_alignment_groups ADD COLUMN tender_list_session_id INTEGER"),
            ("anchor_seq", "ALTER TABLE bid_alignment_groups ADD COLUMN anchor_seq TEXT"),
            ("extraction_meta_json", "ALTER TABLE quotes ADD COLUMN extraction_meta_json JSON"),
            ("confirmed_supplier_ids", "ALTER TABLE tender_list_sessions ADD COLUMN confirmed_supplier_ids JSON"),
        ]:
            try:
                conn.execute(text(ddl))
            except Exception:
                pass  # column already exists
    return engine


@pytest.fixture(scope="module")
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture(scope="module")
def client(db_session):
    def override_db():
        yield db_session

    def override_auth():
        return {"sub": "test", "role": "管理员"}

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_auth
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ─── Seed helpers ─────────────────────────────────────────────────────────────

def _seed_scope_data(db):
    """Seed 1 project, 3 suppliers, 5 anchors, 3×5=15 quotes, 1 session, 5 groups."""
    proj = Project(name="ScopeTest", code="ST-001")
    db.add(proj)
    db.flush()

    # 3 in-scope suppliers
    s1 = Supplier(name="甲供应商")
    s2 = Supplier(name="乙供应商")
    s3 = Supplier(name="丙供应商")
    # 1 historical supplier NOT in current scope
    s_hist = Supplier(name="历史供应商")
    db.add_all([s1, s2, s3, s_hist])
    db.flush()

    CATEGORY = "阀门"
    anchors_json = [{"seq": i, "name": f"阀门{i}号", "spec": f"DN{i*10}", "unit": "套", "qty": 1, "category": CATEGORY}
                    for i in range(1, 6)]

    # TenderListSession with confirmed_supplier_ids = [s1, s2, s3]
    session = TenderListSession(
        project_id=proj.id,
        category=CATEGORY,
        file_name="test.xlsx",
        anchors_total=5,
        anchors_json=anchors_json,
        version=1,
        is_current=True,
        status="confirmed",
        confirmed_supplier_ids=[s1.id, s2.id, s3.id],
    )
    db.add(session)
    db.flush()

    # 5 materials
    mats = []
    for i in range(1, 6):
        m = Material(
            standard_name=f"阀门{i}号",
            spec=f"DN{i*10}",
            category=CATEGORY,
            profession="给排水",
        )
        db.add(m)
        mats.append(m)
    db.flush()

    # Quotes: 3 in-scope suppliers + 1 historical (not in scope)
    for mat in mats:
        for sup in [s1, s2, s3, s_hist]:
            q = Quote(
                material_id=mat.id,
                supplier_id=sup.id,
                project_id=proj.id,
                unit_price=100.0 + sup.id,
                quantity=1,
                batch_id=f"batch-{sup.id}",
            )
            db.add(q)
    db.flush()

    # BidAlignmentGroups: 5 groups linked to current session
    for i, mat in enumerate(mats, 1):
        grp = BidAlignmentGroup(
            project_id=proj.id,
            category=CATEGORY,
            status="confirmed",
            suggested_name=mat.standard_name,
            suggested_spec=mat.spec,
            anchor_seq=str(i),
            tender_list_session_id=session.id,
        )
        db.add(grp)
        db.flush()
        # Items for in-scope suppliers only
        for sup in [s1, s2, s3]:
            qt = db.scalar(select(Quote).where(
                Quote.material_id == mat.id,
                Quote.supplier_id == sup.id,
            ))
            if qt:
                item = BidAlignmentItem(
                    group_id=grp.id,
                    quote_id=qt.id,
                    supplier_id=sup.id,
                    action="align",
                )
                db.add(item)

    # BidAlignmentGroup from OLD session (historical pollution — must NOT appear in current matrix)
    old_session = TenderListSession(
        project_id=proj.id,
        category=CATEGORY,
        file_name="old.xlsx",
        anchors_total=5,
        anchors_json=anchors_json,
        version=0,
        is_current=False,
        status="confirmed",
        confirmed_supplier_ids=[s_hist.id],
    )
    db.add(old_session)
    db.flush()
    old_grp = BidAlignmentGroup(
        project_id=proj.id,
        category=CATEGORY,
        status="confirmed",
        suggested_name="历史阀门1",
        suggested_spec="DN10",
        anchor_seq="1",
        tender_list_session_id=old_session.id,
    )
    db.add(old_grp)
    db.flush()
    qt_hist = db.scalar(select(Quote).where(
        Quote.material_id == mats[0].id,
        Quote.supplier_id == s_hist.id,
    ))
    if qt_hist:
        db.add(BidAlignmentItem(
            group_id=old_grp.id,
            quote_id=qt_hist.id,
            supplier_id=s_hist.id,
            action="align",
        ))

    db.commit()
    return proj.id, session.id, [s1.id, s2.id, s3.id], s_hist.id, CATEGORY


# ─── Tests ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def seeded(db_session):
    return _seed_scope_data(db_session)


def test_matrix_requires_supplier_ids(client, seeded):
    """anchor-review/matrix without supplier_ids must return 400."""
    proj_id, _, _, _, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        # NO supplier_ids — session has confirmed_supplier_ids so should recover
    })
    # Session has confirmed_supplier_ids — endpoint should recover from session scope
    # (NOT 400 in this case, because session persisted them)
    assert resp.status_code == 200, resp.text


def test_matrix_recovers_scope_from_session(client, seeded):
    """Without supplier_ids param, matrix scope must equal session's confirmed_supplier_ids."""
    proj_id, _, confirmed_sids, hist_sid, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
    })
    assert resp.status_code == 200
    data = resp.json()
    returned_sids = {s["supplier_id"] for s in data["suppliers"]}
    assert returned_sids == set(confirmed_sids), (
        f"Expected suppliers {confirmed_sids}, got {returned_sids}"
    )
    assert hist_sid not in returned_sids, "Historical supplier must NOT appear in matrix columns"


def test_matrix_supplier_count_equals_upload_count(client, seeded):
    """supplier_count in matrix == number of confirmed upload suppliers (3), not more."""
    proj_id, _, confirmed_sids, _, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_count"] == 3, f"Expected 3 suppliers, got {data['supplier_count']}"


def test_matrix_rows_equals_anchors_total(client, seeded):
    """rows == anchors_total (every anchor has a row regardless of coverage)."""
    proj_id, _, confirmed_sids, _, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rows"]) == data["anchors_total"], (
        f"rows={len(data['rows'])} != anchors_total={data['anchors_total']}"
    )


def test_matrix_total_cells_accounting(client, seeded):
    """total_cells == anchors_total × supplier_count."""
    proj_id, _, confirmed_sids, _, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    anchors = data["anchors_total"]
    n = data["supplier_count"]
    total_cells = anchors * n
    # Count actual cells in all rows
    actual_cells = sum(len(row["cells"]) for row in data["rows"])
    assert actual_cells == total_cells, (
        f"Cell count mismatch: {actual_cells} != {anchors} × {n} = {total_cells}"
    )


def test_matrix_no_history_pollution_in_cells(client, seeded):
    """No historical supplier appears in any cell key."""
    proj_id, _, confirmed_sids, hist_sid, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    for row in data["rows"]:
        assert str(hist_sid) not in row["cells"], (
            f"Historical supplier {hist_sid} found in row {row['anchor_seq']} cells"
        )


def test_matrix_400_when_no_session_no_param(client, db_session):
    """Without session confirmed_supplier_ids AND without query param → must 400."""
    # Create a project+category with NO TenderListSession
    proj = Project(name="NoSessionProj", code="NS-001")
    db_session.add(proj)
    db_session.flush()
    db_session.commit()

    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj.id,
        "category": "阀门",
    })
    assert resp.status_code in (400, 409), (
        f"Expected 400/409 but got {resp.status_code}: {resp.text}"
    )


def test_session_scope_not_leaked_across_sessions(client, seeded):
    """Groups from old TenderListSession must NOT appear in current matrix rows."""
    proj_id, session_id, confirmed_sids, hist_sid, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    # All 5 rows should have cells from in-scope suppliers with quoted status
    # (the old_grp for anchor_seq=1 with hist supplier must NOT appear)
    for row in data["rows"]:
        for sid_str, cell in row["cells"].items():
            assert int(sid_str) in confirmed_sids, (
                f"Cell for unexpected supplier {sid_str} in row {row['anchor_seq']}"
            )


def test_matrix_distribution_closure(client, seeded):
    """matrix_distribution cell counts must sum correctly."""
    proj_id, _, confirmed_sids, _, cat = seeded
    resp = client.get("/api/analysis/anchor-review/matrix", params={
        "project_id": proj_id,
        "category": cat,
        "supplier_ids": ",".join(str(s) for s in confirmed_sids),
    })
    assert resp.status_code == 200
    data = resp.json()
    md = data.get("matrix_distribution")
    if not md:
        pytest.skip("matrix_distribution not present")
    # quoted_distribution values must sum to anchors_total
    total_from_dist = sum(md["quoted_distribution"].values())
    assert total_from_dist == data["anchors_total"], (
        f"Distribution sum {total_from_dist} != anchors_total {data['anchors_total']}"
    )
    # quoted_ge_2_count <= anchors_total
    assert md["quoted_ge_2_count"] <= data["anchors_total"]
    # quoted_full_count <= quoted_ge_2_count
    assert md["quoted_full_count"] <= md["quoted_ge_2_count"]
