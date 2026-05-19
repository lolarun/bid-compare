"""Integration tests for the invite flow.

Plan §11 / Phase 2 validation (backend API only):
1. POST /api/intake/upload?type=tender returns job_id; job reaches DONE.
2. POST /api/invite/recommend with tender_items → non-empty list,
   TOP-1 is in top-3 historical winners for that category.
3. POST /api/invite/save → bid_invitations rows persisted.

Uses MockProvider (no LLM cost) + isolated SQLite DB.
Seeds suppliers + quotes from docs/data/ CSVs so recommendations have
real signal to work with.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import func

from apps.api.models import (
    Material,
    Project,
    Quote,
    Supplier,
    PROFESSION_MAP,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = REPO_ROOT / "docs" / "data"


# ─── seed helpers ──────────────────────────────────────────────────────────
def _seed_from_csv(db, csv_path: Path, category: str, max_rows: int = 80):
    """Import a subset of a real CSV into the test DB.

    Lightweight version of scripts/import_data.py — just enough to give
    the recommender real history to score against.
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["序号"].notna()].head(max_rows).copy()
    if df.empty:
        return 0

    # Find columns robustly
    def find(*keywords):
        for c in df.columns:
            cs = str(c)
            if all(k in cs for k in keywords):
                return c
        return None

    name_col = find("项目名称") or find("名称")
    brand_col = find("品牌")
    qty_col = find("数量")
    # Try multiple price columns
    price_col = find("价税合计") or find("含税单价") or find("单价")

    if not name_col:
        return 0

    project = Project(name=f"测试种子项目_{category}")
    db.add(project)
    db.flush()

    profession = PROFESSION_MAP.get(category, "其他")
    count = 0
    for i, row in df.iterrows():
        raw_name = row.get(name_col)
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        mat = Material(
            material_code=f"TEST-{category}-{i:04d}",
            standard_name=str(raw_name).strip(),
            profession=profession,
            category=category,
            sub_category="",
            spec="",
            unit="个",
            ref_price_reasonable_low=100.0,
            ref_price_median=120.0,
        )
        db.add(mat)
        db.flush()

        brand = str(row.get(brand_col)).strip() if brand_col and pd.notna(row.get(brand_col)) else ""
        if brand and brand not in {"nan", "None"}:
            supplier = db.query(Supplier).filter_by(name=brand).first()
            if not supplier:
                supplier = Supplier(name=brand)
                db.add(supplier)
                db.flush()
        else:
            supplier = None

        price = None
        if price_col and pd.notna(row.get(price_col)):
            try:
                price = float(row.get(price_col))
            except (TypeError, ValueError):
                price = None
        qty = None
        if qty_col and pd.notna(row.get(qty_col)):
            try:
                qty = float(row.get(qty_col))
            except (TypeError, ValueError):
                qty = None

        if price is None or price <= 0:
            continue

        q = Quote(
            material_id=mat.id,
            supplier_id=supplier.id if supplier else None,
            project_id=project.id,
            unit_price=price,
            quantity=qty,
            brand=brand,
            # Fake deviation: every supplier gets ~3% over reasonable_low
            deviation_pct=(price - 100.0) / 100.0 if price else None,
        )
        db.add(q)
        count += 1
    db.commit()
    return count


@pytest.fixture
def seeded_client(temp_db, monkeypatch, fixture_dir, tmp_path):
    """TestClient with seeded DB + MockProvider."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )

    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider

    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(MockProvider(fixture_dir=fixture_dir)),
    )

    # Seed DB BEFORE app boots (we use temp_db's engine)
    _, SessionLocal = temp_db
    db = SessionLocal()
    try:
        # Cable trays (桥架) — biggest category
        bridge_csv = DATA_DIR / "桥架报价单格式模板_汇总.csv"
        if bridge_csv.exists():
            _seed_from_csv(db, bridge_csv, "桥架", max_rows=120)
        # Valves
        valves_csv = DATA_DIR / "阀门询价格式_汇总.csv"
        if valves_csv.exists():
            _seed_from_csv(db, valves_csv, "阀门", max_rows=80)
    finally:
        db.close()

    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


# ─── tests ─────────────────────────────────────────────────────────────────
class TestPhase2InviteFlow:
    """Plan §11 Phase 2 validation: 3 endpoints reachable + correct."""

    def test_intake_upload_tender_reaches_done(self, seeded_client):
        r = seeded_client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("tender.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        # Poll (TestClient awaits BackgroundTasks synchronously)
        r2 = seeded_client.get(f"/api/intake/jobs/{job_id}")
        body = r2.json()
        assert body["status"] == "done", body
        assert body["result"] is not None
        assert "items" in body["result"]

    def test_recommend_returns_non_empty_with_correct_top1(self, seeded_client):
        """TOP-1 must be in the top-3 historical winners for the category."""
        # Construct tender items for 桥架 category
        tender_items = [
            {"name": "电缆桥架 300×200", "category": "桥架", "qty": 100, "unit": "米"},
            {"name": "电缆桥架 200×100", "category": "桥架", "qty": 80, "unit": "米"},
        ]
        r = seeded_client.post(
            "/api/invite/recommend",
            json={"tender_items": tender_items, "top_n": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["categories"] == ["桥架"]
        recs = body["recommendations"]
        assert len(recs) > 0, "Should return ≥1 recommendation"
        assert len(recs) <= 5

        # Ranks should be 1..N
        assert [r["rank"] for r in recs] == list(range(1, len(recs) + 1))
        # All scores should be 0..100
        for r in recs:
            assert 0 <= r["score"] <= 100
            assert "summary" in r["reason"]

        # TOP-1 must be in top-3 historical winners for 桥架 by quote count
        from apps.api.core.database import SessionLocal
        db = SessionLocal()
        try:
            top_historical = (
                db.query(Supplier.id)
                .join(Quote, Quote.supplier_id == Supplier.id)
                .join(Material, Material.id == Quote.material_id)
                .filter(Material.category == "桥架", Quote.unit_price > 0)
                .group_by(Supplier.id)
                .order_by(func.count(Quote.id).desc())
                .limit(3)
                .all()
            )
            top3_ids = {row[0] for row in top_historical}
        finally:
            db.close()

        top1_id = recs[0]["supplier_id"]
        assert top1_id in top3_ids, (
            f"TOP-1 recommended supplier {top1_id} ({recs[0]['supplier_name']}) "
            f"not in top-3 historical winners {top3_ids}"
        )

    def test_save_persists_bid_invitations(self, seeded_client):
        # Get recommendations first
        r = seeded_client.post(
            "/api/invite/recommend",
            json={"tender_items": [{"name": "桥架300", "category": "桥架"}], "top_n": 3},
        )
        recs = r.json()["recommendations"]
        assert recs, "Pre-requisite: recommend must return results"
        supplier_ids = [x["supplier_id"] for x in recs[:2]]

        save_body = {
            "project_name": "Phase 2 测试项目",
            "project_code": "P2-TEST",
            "items": [{"name": "桥架300", "category": "桥架", "quantity": 100}],
            "supplier_ids": supplier_ids,
        }
        r2 = seeded_client.post("/api/invite/save", json=save_body)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        tender_id = body["tender_id"]
        assert tender_id > 0
        assert len(body["invitations"]) == 2

        # Verify DB rows
        from apps.api.core.database import SessionLocal
        from apps.api.models import BidInvitation, TenderDocument
        db = SessionLocal()
        try:
            tender = db.get(TenderDocument, tender_id)
            assert tender is not None
            assert tender.status == "invited"
            invs = db.query(BidInvitation).filter_by(tender_id=tender_id).all()
            assert len(invs) == 2
            for inv in invs:
                assert inv.supplier_id in supplier_ids
                assert inv.score is not None
                assert inv.rank is not None
                assert inv.reason  # dict with summary etc.
        finally:
            db.close()

    def test_idempotent_save_no_duplicates(self, seeded_client):
        """Saving the same supplier twice for the same tender should not create duplicates."""
        r = seeded_client.post(
            "/api/invite/recommend",
            json={"tender_items": [{"name": "阀门 DN50", "category": "阀门"}], "top_n": 2},
        )
        recs = r.json()["recommendations"]
        if not recs:
            pytest.skip("No 阀门 candidates in seeded data — skip duplicate test")
        sup_id = recs[0]["supplier_id"]

        body = {
            "project_name": "Dedup Test",
            "items": [{"name": "阀门 DN50", "category": "阀门"}],
            "supplier_ids": [sup_id],
        }
        r1 = seeded_client.post("/api/invite/save", json=body)
        tender_id = r1.json()["tender_id"]

        # Second save with same tender_id + same supplier
        body2 = {**body, "tender_id": tender_id, "supplier_ids": [sup_id]}
        r2 = seeded_client.post("/api/invite/save", json=body2)
        assert r2.status_code == 200

        from apps.api.core.database import SessionLocal
        from apps.api.models import BidInvitation
        db = SessionLocal()
        try:
            count = db.query(BidInvitation).filter_by(
                tender_id=tender_id, supplier_id=sup_id
            ).count()
            assert count == 1, f"Expected 1 invitation row, got {count}"
        finally:
            db.close()


class TestInferCategories:
    """Unit test of the category-inference helper."""

    def test_explicit_category(self):
        from apps.api.services.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "X", "category": "桥架"}])
        assert cats == ["桥架"]

    def test_name_keyword_match(self):
        from apps.api.services.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "桥架300×200 镀锌"}])
        assert cats == ["桥架"]

    def test_dedupe(self):
        from apps.api.services.supplier_recommend import infer_categories
        cats = infer_categories([
            {"name": "桥架A"},
            {"name": "桥架B"},
            {"name": "阀门 DN100"},
        ])
        assert cats == ["桥架", "阀门"]

    def test_unknown_ignored(self):
        from apps.api.services.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "随便起的名字"}])
        assert cats == []
