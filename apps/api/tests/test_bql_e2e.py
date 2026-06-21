"""E2E: batch-confirm → BidQuoteLine → align → anchor_review → matrix → archive-prices.

Asserts the full new-path flow never creates a Quote until archive-prices is called,
and that all review/matrix endpoints expose bid_quote_line_id in their responses.
"""

import uuid
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.core.database import Base
from apps.api.main import app
from apps.api.core.database import get_db
from apps.api.routes.auth import get_current_user
from apps.api.models import (
    Project, Supplier, Material, Quote, TenderListSession, ExtractionJob,
)
from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
from apps.api.models.bid_submission import BidSubmission, BidQuoteLine


# ─── Module-scoped in-memory DB ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # all connections share the same in-memory database
    )
    # Force all models to load so they register with Base before create_all
    import apps.api.models  # noqa: F401
    Base.metadata.create_all(engine)
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


# ─── Seed ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def seed(db_session):
    """Seed: project, supplier, 3 materials (so 3 BQLs get material_id, 2 are NULL)."""
    proj = Project(name="BQL-E2E-Project", code="BQL-001")
    db_session.add(proj)
    db_session.flush()

    sup = Supplier(name="BQL测试供应商", merge_status="active")
    db_session.add(sup)
    db_session.flush()

    CATEGORY = "阀门"
    mats = []
    for i in range(1, 4):
        m = Material(
            standard_name=f"BQL阀门{i}号",
            spec=f"DN{i * 10}",
            category=CATEGORY,
            profession="给排水",
        )
        db_session.add(m)
        mats.append(m)
    db_session.flush()

    # 5 anchors — first 3 match a Material, last 2 will have material_id=NULL
    anchors_json = [
        {"seq": str(i), "name": f"BQL阀门{i}号", "spec": f"DN{i*10}",
         "unit": "套", "qty": 1, "category": CATEGORY}
        for i in range(1, 6)
    ]
    session = TenderListSession(
        project_id=proj.id,
        category=CATEGORY,
        file_name="bql-test.pdf",
        anchors_total=5,
        anchors_json=anchors_json,
        version=1,
        is_current=True,
        status="confirmed",
        source_type="pdf_primary",
        confirmed_supplier_ids=[sup.id],
    )
    db_session.add(session)
    db_session.flush()

    # ExtractionJob — result.items will be passed via overrides
    job_id = str(uuid.uuid4())
    job = ExtractionJob(
        id=job_id,
        type="quote",
        status="done",
        result={"items": [], "supplier_name": "BQL测试供应商"},
        context={"category": CATEGORY},
        filename="bql-test.pdf",
    )
    db_session.add(job)
    db_session.flush()

    db_session.commit()

    return {
        "proj": proj,
        "sup": sup,
        "mats": mats,
        "session": session,
        "job_id": job_id,
        "category": CATEGORY,
    }


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestBqlE2E:

    def test_batch_confirm_creates_bql_not_quote(self, client, db_session, seed):
        """batch_confirm must create BidSubmission + BidQuoteLines — zero Quotes."""
        proj = seed["proj"]
        sup = seed["sup"]
        mats = seed["mats"]
        category = seed["category"]

        # 5 items: 3 match materials by standard_name, 2 are unknown
        overrides = [
            {
                "material": m.standard_name, "standard_name": m.standard_name,
                "spec": m.spec, "unit": "套", "qty": 1,
                "unit_price": 100.0 + i, "total_price": 100.0 + i,
                "brand": "开滋", "category": category,
            }
            for i, m in enumerate(mats)
        ] + [
            {
                "material": f"未知阀门{j}", "standard_name": f"未知阀门{j}",
                "spec": "DN999", "unit": "套", "qty": 1,
                "unit_price": 200.0, "total_price": 200.0,
                "brand": "", "category": category,
            }
            for j in range(1, 3)
        ]

        r = client.post("/api/quotes/batch-confirm", json={
            "job_id": seed["job_id"],
            "supplier_id": sup.id,
            "supplier_name": sup.name,
            "project_id": proj.id,
            "overrides": overrides,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["line_count"] == 5, data
        assert data["supplier_id"] == sup.id

        # P0 invariant: NO Quote created during batch_confirm
        quote_count = db_session.query(Quote).count()
        assert quote_count == 0, "batch_confirm must not create any Quote"

        # BidSubmission + 5 BidQuoteLines created
        submission = db_session.get(BidSubmission, data["submission_id"])
        assert submission is not None
        assert submission.supplier_id == sup.id
        lines = db_session.query(BidQuoteLine).filter_by(
            submission_id=submission.id
        ).all()
        assert len(lines) == 5

        # 3 lines have material_id (matched), 2 are NULL
        with_mid = [l for l in lines if l.material_id is not None]
        without_mid = [l for l in lines if l.material_id is None]
        assert len(with_mid) == 3, f"Expected 3 matched, got {len(with_mid)}"
        assert len(without_mid) == 2

        # Prices are preserved
        assert all(l.unit_price is not None for l in with_mid)

        # Store submission_id for later tests
        seed["submission_id"] = submission.id
        seed["bql_lines"] = lines

    def test_tender_list_match_creates_bql_items(self, client, db_session, seed):
        """POST /tender-list/match must create BidAlignmentItem rows with bid_quote_line_id
        (not quote_id) when a BidSubmission exists for the matching supplier."""
        proj = seed["proj"]
        sup = seed["sup"]
        category = seed["category"]
        session = seed["session"]

        r = client.post(
            "/api/analysis/tender-list/match",
            data={
                "project_id": str(proj.id),
                "category": category,
                "supplier_ids": str(sup.id),
            },
        )
        assert r.status_code == 200, r.text

        # Groups must be created
        from apps.api.models.bid_alignment import BidAlignmentGroup, BidAlignmentItem
        groups = db_session.query(BidAlignmentGroup).filter_by(
            project_id=proj.id, category=category
        ).all()
        assert len(groups) > 0, "No BidAlignmentGroup created by match endpoint"

        # All items must come from BQL (bid_quote_line_id set, quote_id NULL)
        all_items = []
        for g in groups:
            items = db_session.query(BidAlignmentItem).filter_by(group_id=g.id).all()
            all_items.extend(items)

        assert len(all_items) > 0, "No BidAlignmentItem created"

        bql_items = [it for it in all_items if it.bid_quote_line_id is not None]
        quote_items = [it for it in all_items if it.quote_id is not None]
        assert len(bql_items) > 0, "Match endpoint produced no BQL-path items"
        assert len(quote_items) == 0, (
            f"Match endpoint produced {len(quote_items)} old-path (quote_id) items — "
            "supplier has BidSubmission, must use BQL exclusively"
        )

        # Store the group id for downstream tests
        seed["group_id"] = groups[0].id

    def test_anchor_review_sees_bql_items(self, client, db_session, seed):
        """anchor_review must return bid_quote_line_id in item detail for new-path items."""
        proj = seed["proj"]
        sup = seed["sup"]
        category = seed["category"]

        r = client.get(
            "/api/analysis/anchor-review",
            params={
                "project_id": proj.id,
                "category": category,
                "supplier_ids": str(sup.id),
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()

        # Should have groups (confirmed or low-conf)
        all_groups = data.get("confirmed_groups", []) + data.get("low_conf_groups", [])
        assert len(all_groups) > 0, "No groups returned from anchor_review"

        # Every item in new-path groups must have bid_quote_line_id (not quote_id)
        new_path_items = []
        for g in all_groups:
            for it in g.get("items", []):
                if it.get("bid_quote_line_id") is not None:
                    new_path_items.append(it)
                    assert it.get("quote_id") is None, "New-path item must not have quote_id"
                    assert it.get("unit_price") is not None, "BQL item must carry unit_price"
                    assert it.get("supplier_name") != "", "BQL item must resolve supplier_name"

        assert len(new_path_items) > 0, "No new-path (BQL) items found in anchor_review"

    def test_bid_matrix_cells_have_bql_id(self, client, db_session, seed):
        """bid_matrix cells built from BQL items must carry bid_quote_line_id, not source_quote_id."""
        from apps.api.services.bid_matrix import build_anchor_review_matrix
        from apps.api.models.tender_list_session import TenderListSession
        from apps.api.services.tender_list import TenderAnchor

        proj = seed["proj"]
        sup = seed["sup"]
        session = seed["session"]
        category = seed["category"]

        from sqlalchemy import text
        engine = db_session.bind
        with engine.connect() as conn:
            anchors_raw = conn.execute(
                text("SELECT anchors_json FROM tender_list_sessions WHERE id = :sid"),
                {"sid": session.id}
            ).fetchone()

        anchors_json = json.loads(anchors_raw[0]) if isinstance(anchors_raw[0], str) else anchors_raw[0]
        anchors = [
            TenderAnchor(
                seq=str(a["seq"]),
                name=a["name"],
                spec=a.get("spec", ""),
                unit=a.get("unit", "套"),
                qty=a.get("qty", 1),
            )
            for a in (anchors_json or [])[:1]  # just test first anchor
        ]

        submission_id = seed["submission_id"]
        result = build_anchor_review_matrix(
            db=db_session,
            project_id=proj.id,
            category=category,
            submission_ids=[submission_id],
        )
        assert result is not None

        # Verify cells for submission carry bid_quote_line_id (BQL path)
        rows = result.get("rows", [])
        bql_cells_found = False
        for row in rows:
            cell = row["cells"].get(str(submission_id), {})
            if cell.get("bid_quote_line_id") is not None:
                bql_cells_found = True
                assert cell.get("unit_price") is not None, "BQL cell must carry unit_price"
                break
        assert bql_cells_found, "No matrix cells with bid_quote_line_id found"

    def test_archive_prices_creates_quotes_for_matched_bqls(self, client, db_session, seed):
        """archive-prices must create Quote only for BQL lines where material_id IS NOT NULL."""
        submission_id = seed["submission_id"]
        lines = seed["bql_lines"]

        eligible_count = len([l for l in lines if l.material_id is not None
                              and l.archived_quote_id is None])

        r = client.post("/api/quotes/archive-prices", json={
            "submission_id": submission_id,
        })
        assert r.status_code == 200, r.text
        data = r.json()

        assert data["status"] in ("archived", "partially_archived"), data
        assert data["archived_count"] == eligible_count, data
        assert data["skipped_count"] == len(lines) - eligible_count

        # Exactly `eligible_count` Quotes should now exist (were 0 before)
        quote_count = db_session.query(Quote).count()
        assert quote_count == eligible_count, (
            f"Expected {eligible_count} quotes after archive, got {quote_count}"
        )

        # BQL.archived_quote_id must be set for archived lines
        db_session.expire_all()
        archived_lines = db_session.query(BidQuoteLine).filter(
            BidQuoteLine.submission_id == submission_id,
            BidQuoteLine.archived_quote_id.isnot(None),
        ).all()
        assert len(archived_lines) == eligible_count

    def test_no_quote_in_old_path_columns(self, db_session, seed):
        """BidAlignmentItem rows for new path must have quote_id=NULL."""
        group_id = seed["group_id"]
        items = db_session.query(BidAlignmentItem).filter_by(group_id=group_id).all()
        assert len(items) > 0

        for item in items:
            # New-path items: bid_quote_line_id set, quote_id null
            assert item.bid_quote_line_id is not None, "item missing bid_quote_line_id"
            assert item.quote_id is None, "new-path item must have quote_id=NULL"


# ─── Round 3 additions ────────────────────────────────────────────────────────

@pytest.fixture
def unit_session(db_engine):
    """Function-scoped session sharing the module-level engine but rolling back after each test.

    New-test classes use this instead of the module-scoped db_session so that
    failed/dirty flushes don't contaminate the shared TestBqlE2E session.
    """
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


def _make_job(session, suffix: str) -> str:
    """Create a minimal ExtractionJob and return its id."""
    import uuid as _uuid
    jid = str(_uuid.uuid4())
    session.add(ExtractionJob(
        id=jid, type="quote", status="done",
        result={"items": []}, context={}, filename=f"test-{suffix}.pdf",
    ))
    session.flush()
    return jid


class TestResolveActiveSubmissions:
    """Unit tests for resolve_active_submissions() shared service."""

    def test_excludes_rejected_submission(self, unit_session):
        """resolve_active_submissions must not return rejected submissions."""
        from apps.api.services.bid_submission_resolve import resolve_active_submissions

        proj = Project(name="RAR-test", code="RAR-001")
        unit_session.add(proj)
        unit_session.flush()
        sup = Supplier(name="RAR供应商", merge_status="active")
        unit_session.add(sup)
        unit_session.flush()

        jid = _make_job(unit_session, "rar")
        sub_rejected = BidSubmission(
            job_id=jid, project_id=proj.id, supplier_id=sup.id,
            batch_id="rar-rej", status="rejected",
        )
        unit_session.add(sub_rejected)
        unit_session.flush()

        unit_session.add(BidQuoteLine(
            submission_id=sub_rejected.id, standard_name="闸阀DN50",
            spec="DN50", unit="个", qty=1, unit_price=100, category="阀门",
        ))
        unit_session.flush()

        result = resolve_active_submissions(unit_session, proj.id, "阀门")
        assert sup.id not in result, "rejected submission must be excluded"

    def test_requires_bql_rows_for_category(self, unit_session):
        """Supplier with BQL in wrong category must not appear."""
        from apps.api.services.bid_submission_resolve import resolve_active_submissions

        proj = Project(name="RC-test", code="RC-001")
        unit_session.add(proj)
        unit_session.flush()
        sup = Supplier(name="RC供应商", merge_status="active")
        unit_session.add(sup)
        unit_session.flush()

        jid = _make_job(unit_session, "rc")
        sub = BidSubmission(
            job_id=jid, project_id=proj.id, supplier_id=sup.id,
            batch_id="rc-sub", status="pending",
        )
        unit_session.add(sub)
        unit_session.flush()
        unit_session.add(BidQuoteLine(
            submission_id=sub.id, standard_name="桥架", spec="300×200",
            unit="m", qty=1, unit_price=50, category="桥架",
        ))
        unit_session.flush()

        result = resolve_active_submissions(unit_session, proj.id, "阀门")
        assert sup.id not in result, "supplier with only wrong-category BQL must be excluded"

        result2 = resolve_active_submissions(unit_session, proj.id, "桥架")
        assert sup.id in result2, "supplier must appear for the correct category"

    def test_latest_wins_among_multiple_submissions(self, unit_session):
        """Among multiple valid submissions per supplier, latest id wins."""
        from apps.api.services.bid_submission_resolve import resolve_active_submissions

        proj = Project(name="LW-test", code="LW-001")
        unit_session.add(proj)
        unit_session.flush()
        sup = Supplier(name="LW供应商", merge_status="active")
        unit_session.add(sup)
        unit_session.flush()

        jid1 = _make_job(unit_session, "lw1")
        sub1 = BidSubmission(
            job_id=jid1, project_id=proj.id, supplier_id=sup.id,
            batch_id="lw-sub1", status="pending",
        )
        unit_session.add(sub1)
        unit_session.flush()
        unit_session.add(BidQuoteLine(
            submission_id=sub1.id, standard_name="闸阀DN50", spec="DN50",
            unit="个", qty=1, unit_price=100, category="阀门",
        ))

        jid2 = _make_job(unit_session, "lw2")
        sub2 = BidSubmission(
            job_id=jid2, project_id=proj.id, supplier_id=sup.id,
            batch_id="lw-sub2", status="pending",
        )
        unit_session.add(sub2)
        unit_session.flush()
        unit_session.add(BidQuoteLine(
            submission_id=sub2.id, standard_name="闸阀DN50", spec="DN50",
            unit="个", qty=1, unit_price=120, category="阀门",
        ))
        unit_session.flush()

        # resolve_active_submissions is now keyed by submission_id (not supplier_id).
        # Both submissions are returned; verify both are present and keyed correctly.
        result = resolve_active_submissions(unit_session, proj.id, "阀门")
        assert sub1.id in result, "sub1 must be in result"
        assert sub2.id in result, "sub2 must be in result"
        assert result[sub1.id].supplier_id == sup.id
        assert result[sub2.id].supplier_id == sup.id


class TestUsedSubmissionIdsPersisted:
    """Verify tender-list/match persists used_submission_ids on TenderListSession."""

    def test_used_submission_ids_written_after_match(self, client, db_session, seed):
        """After /tender-list/match, TLS.used_submission_ids must list the BQL submission id."""
        from apps.api.models.tender_list_session import TenderListSession

        proj = seed["proj"]
        category = seed["category"]
        session = seed["session"]

        # Re-run match (idempotent — clears and recreates groups)
        r = client.post(
            "/api/analysis/tender-list/match",
            data={
                "project_id": str(proj.id),
                "category": category,
                "supplier_ids": str(seed["sup"].id),
            },
        )
        assert r.status_code == 200, r.text

        db_session.expire_all()
        tls = db_session.get(TenderListSession, session.id)
        assert tls is not None
        used = tls.used_submission_ids or []
        assert len(used) >= 1, "used_submission_ids must be persisted after match"
        assert seed["submission_id"] in used, (
            f"submission_id {seed['submission_id']} must be in used_submission_ids {used}"
        )


class TestArchiveStatusEdgeCases:
    """Archive status must reflect the correct three-state logic."""

    def test_no_eligible_when_all_null_material(self, client, db_session, seed):
        """A submission with ONLY null-material lines must return status=no_eligible."""
        proj = seed["proj"]
        sup = seed["sup"]
        category = seed["category"]

        # Create a fresh job + submission with only null-material lines
        job2_id = _make_job(db_session, "null-only")
        sub2 = BidSubmission(
            job_id=job2_id, supplier_id=sup.id, project_id=proj.id,
            batch_id=f"NULL-ONLY-{job2_id[:8]}", status="pending",
        )
        db_session.add(sub2)
        db_session.flush()

        for i in range(3):
            db_session.add(BidQuoteLine(
                submission_id=sub2.id, standard_name=f"Unknown{i}",
                spec="???", unit="个", qty=1, unit_price=50,
                category=category, material_id=None,
            ))
        db_session.commit()

        r = client.post("/api/quotes/archive-prices", json={"submission_id": sub2.id})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "no_eligible", (
            f"All null-material lines must yield no_eligible, got: {data['status']}"
        )
        assert data["archived_count"] == 0
        assert data["eligible_count"] == 0


class TestLoadSupplierFillRowsBQL:
    """_load_supplier_fill_rows must read BQL when BidSubmission exists."""

    def test_reads_bql_not_quote_when_submission_exists(self, db_session, seed):
        """When an active BidSubmission exists, rows must have bid_quote_line_id set."""
        from apps.api.routes.analysis import _load_supplier_fill_rows

        proj = seed["proj"]
        sup = seed["sup"]
        category = seed["category"]

        rows = _load_supplier_fill_rows(db_session, proj.id, category, sup.id)
        assert len(rows) > 0, "must return rows when BidSubmission exists"

        bql_rows = [r for r in rows if r.bid_quote_line_id is not None]
        quote_rows = [r for r in rows if r.bid_quote_line_id is None]
        assert len(bql_rows) == len(rows), (
            f"All rows must come from BQL (bid_quote_line_id set). "
            f"Got {len(quote_rows)} non-BQL rows."
        )

    def test_falls_back_to_empty_when_no_submission(self, unit_session):
        """When no BidSubmission exists for a supplier, must return empty list (no Quote fallback)."""
        from apps.api.routes.analysis import _load_supplier_fill_rows

        proj = Project(name="NOSUB-test", code="NOSUB-001")
        unit_session.add(proj)
        unit_session.flush()
        legacy_sup = Supplier(name="LegacySupNoSub2", merge_status="active")
        unit_session.add(legacy_sup)
        unit_session.flush()

        rows = _load_supplier_fill_rows(unit_session, proj.id, "阀门", legacy_sup.id)
        assert len(rows) == 0, (
            "Supplier with no BidSubmission and no Quote must return empty rows"
        )
