"""Integration tests for the invite flow (brand-based recommendation).

Test tiers:
  L1 unit        — TestInferCategories (no DB, no server)
  L2 integration — TestPhase2InviteFlow / TestBrandRecommendation /
                   TestBrandRecommendJingqiao  (MockProvider + temp SQLite)
  L3 e2e         — TestBrandRecommendE2E  (@pytest.mark.e2e, DASHSCOPE_API_KEY)
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from PIL import Image
from fastapi.testclient import TestClient

from apps.api.models import (
    Material,
    Project,
    Quote,
    Supplier,
    PROFESSION_MAP,
)
from apps.api.models.brand_tier import BrandTier

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR   = REPO_ROOT / "docs" / "data"
FIXTURE_DIR = Path(__file__).parent / "fixtures"

import math as _math
_W_TIER, _W_DATA, _MAX_S = 0.30, 0.70, 50

def _brand_score(rec: dict) -> float:
    tf = 1.0 if rec["tier"] == "合资" else 0.0
    df = min(_math.log(rec["sample_count"] + 1) / _math.log(_MAX_S + 1), 1.0)
    return _W_TIER * tf + _W_DATA * df

# ─── known-brand seed specs: (brand_name, tier, category, n_quotes, price_range) ──
_VALVE_BRANDS = [
    ("KITZ",     "合资", "阀门", 20, (500, 2000)),
    ("WATTS",    "合资", "阀门",  5, (300, 1500)),
    ("上海良工", "国产", "阀门", 15, (200,  800)),
    ("上海冠龙", "国产", "阀门",  0, None),
]

_BRIDGE_BRANDS = [
    ("川汇", "国产", "桥架", 25, ( 50, 200)),
    ("国强", "国产", "桥架", 10, ( 40, 150)),
    ("中孚", "国产", "桥架",  3, ( 60, 180)),
]


# ─── seed helpers ──────────────────────────────────────────────────────────
def _seed_from_csv(db, csv_path: Path, category: str, max_rows: int = 80) -> int:
    """Import a subset of a real CSV into the test DB for supplier-history."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = df[df["序号"].notna()].head(max_rows).copy()
    if df.empty:
        return 0

    def find(*keywords):
        for c in df.columns:
            cs = str(c)
            if all(k in cs for k in keywords):
                return c
        return None

    name_col  = find("项目名称") or find("名称")
    brand_col = find("品牌")
    qty_col   = find("数量")
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

        brand = (
            str(row.get(brand_col)).strip()
            if brand_col and pd.notna(row.get(brand_col))
            else ""
        )
        if brand and brand not in {"nan", "None"}:
            supplier = db.scalar(select(Supplier).where(Supplier.name == brand))
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
            deviation_pct=(price - 100.0) / 100.0 if price else None,
        )
        db.add(q)
        count += 1
    db.commit()
    return count


def _seed_brand_tiers(db, brand_specs: list[tuple]) -> None:
    """Seed BrandTier rows (and matching Quote rows) for recommendation tests.

    Each spec: (brand_name, tier, category, n_quotes, price_range | None)
    Quotes are seeded without a supplier so they pass valid_quote_filters()
    unconditionally (supplier_id IS NULL branch).
    """
    for brand_name, tier, category, n_quotes, price_range in brand_specs:
        bt = BrandTier(
            brand_name=brand_name,
            tier=tier,
            category=category,
            is_approved=True,
            canonical_name=brand_name,
        )
        db.add(bt)
        db.flush()

        if n_quotes and price_range:
            lo, hi = price_range
            mat = Material(
                material_code=f"SEED-{brand_name}-{category}",
                standard_name=f"测试{category}",
                profession=PROFESSION_MAP.get(category, "其他"),
                category=category,
                sub_category="",
                spec="",
                unit="个",
            )
            db.add(mat)
            db.flush()

            project = Project(name=f"种子_{brand_name}")
            db.add(project)
            db.flush()

            step = (hi - lo) / max(n_quotes - 1, 1)
            for i in range(n_quotes):
                db.add(Quote(
                    material_id=mat.id,
                    project_id=project.id,
                    unit_price=lo + i * step,
                    brand=brand_name,
                    # supplier_id omitted → passes valid_quote_filters()
                ))
    db.commit()


# ─── fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture
def seeded_client(temp_db, monkeypatch, fixture_dir, tmp_path):
    """TestClient with seeded DB (brand tiers + CSV history) + MockProvider + auth bypass."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(
        "apps.api.services.ingestion.document_ingestion.UPLOAD_DIR", tmp_path / "uploads"
    )

    from apps.api.intelligence.pipeline import ExtractionPipeline
    from apps.api.intelligence.providers.mock import MockProvider

    monkeypatch.setattr(
        "apps.api.main._build_pipeline",
        lambda: ExtractionPipeline(MockProvider(fixture_dir=fixture_dir)),
    )

    _, SessionLocal = temp_db
    db = SessionLocal()
    try:
        # Seed approved brand tiers (required for recommend_brands)
        _seed_brand_tiers(db, _VALVE_BRANDS)
        _seed_brand_tiers(db, _BRIDGE_BRANDS)
        # Optionally seed historical CSV rows for broader signal
        bridge_csv = DATA_DIR / "桥架报价单格式模板_汇总.csv"
        if bridge_csv.exists():
            _seed_from_csv(db, bridge_csv, "桥架", max_rows=60)
        valves_csv = DATA_DIR / "阀门询价格式_汇总.csv"
        if valves_csv.exists():
            _seed_from_csv(db, valves_csv, "阀门", max_rows=40)
    finally:
        db.close()

    from apps.api.main import app
    from apps.api.routes.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "role": "管理员"}
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def brand_client(temp_db, monkeypatch, tmp_path):
    """Minimal TestClient: brand tiers only, no CSV seed, no MockProvider needed."""
    _, SessionLocal = temp_db
    db = SessionLocal()
    try:
        _seed_brand_tiers(db, _VALVE_BRANDS)
        _seed_brand_tiers(db, _BRIDGE_BRANDS)
    finally:
        db.close()

    from apps.api.main import app
    from apps.api.routes.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {"sub": "test", "role": "管理员"}
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


# ─── TestPhase2InviteFlow ───────────────────────────────────────────────────
class TestPhase2InviteFlow:
    """Phase 2 end-to-end: intake → brand recommend → save."""

    def test_intake_upload_tender_reaches_done(self, seeded_client):
        r = seeded_client.post(
            "/api/intake/upload",
            data={"type": "tender"},
            files={"file": ("tender.png", _png(), "image/png")},
        )
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        r2 = seeded_client.get(f"/api/intake/jobs/{job_id}")
        body = r2.json()
        assert body["status"] == "done", body
        assert body["result"] is not None
        assert "items" in body["result"]

    def test_recommend_returns_brands_for_category(self, seeded_client):
        """Brand recommend: correct shape, non-empty, sorted by sample_count."""
        tender_items = [
            {"name": "电缆桥架 300×200", "category": "桥架", "qty": 100},
            {"name": "电缆桥架 200×100", "category": "桥架", "qty": 80},
        ]
        r = seeded_client.post(
            "/api/invite/recommend",
            json={"tender_items": tender_items, "top_n": 5},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["categories"] == ["桥架"]
        recs = body["recommendations"]
        assert len(recs) > 0, "Should return ≥1 brand recommendation"
        assert len(recs) <= 5

        for rec in recs:
            assert isinstance(rec["brand_name"], str)
            assert rec["tier"] in {"合资", "国产"}
            assert rec["category"] == "桥架"
            assert isinstance(rec["sample_count"], int)
            assert isinstance(rec["tags"], list)
            # When prices exist, p10 ≤ median ≤ p90
            if rec["price_median"] is not None:
                assert rec["price_p10"] is not None
                assert rec["price_p90"] is not None
                assert rec["price_p10"] <= rec["price_median"] <= rec["price_p90"]

        # 桥架种子全是国产，样本最多的川汇应排第一
        assert recs[0]["brand_name"] == "川汇"
        assert recs[0]["sample_count"] == 25
        # 复合分数单调不增
        scores = [_brand_score(r) for r in recs]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], f"score[{i}]={scores[i]:.4f} < score[{i+1}]={scores[i+1]:.4f}"

    def test_save_creates_tender_document(self, seeded_client):
        """Brand-only save: TenderDocument created, invitations empty."""
        save_body = {
            "project_name": "Phase 2 测试项目",
            "project_code": "P2-TEST",
            "items": [{"name": "电缆桥架 300×200", "category": "桥架"}],
            "brand_requirements": ["川汇", "国强"],
        }
        r = seeded_client.post("/api/invite/save", json=save_body)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tender_id"] > 0
        assert body["invitations"] == []

        # Verify DB state
        from apps.api.core.database import SessionLocal
        from apps.api.models import TenderDocument
        db = SessionLocal()
        try:
            tender = db.get(TenderDocument, body["tender_id"])
            assert tender is not None
            assert tender.project_name == "Phase 2 测试项目"
            assert tender.status == "draft"
        finally:
            db.close()

    def test_idempotent_save_reuses_tender(self, seeded_client):
        """Saving with the same tender_id should not create a duplicate."""
        r1 = seeded_client.post("/api/invite/save", json={
            "project_name": "Dedup Test",
            "items": [{"name": "阀门 DN50", "category": "阀门"}],
        })
        assert r1.status_code == 200, r1.text
        tender_id = r1.json()["tender_id"]
        assert tender_id > 0

        # Re-save with explicit tender_id → same tender reused
        r2 = seeded_client.post("/api/invite/save", json={
            "tender_id": tender_id,
            "project_name": "Dedup Test",
            "items": [{"name": "阀门 DN50", "category": "阀门"}],
        })
        assert r2.status_code == 200, r2.text
        assert r2.json()["tender_id"] == tender_id

    def test_resave_with_tender_id_persists_cover_fields(self, seeded_client):
        """R4: re-saving via tender_id must actually update the existing row's
        cover-page scalars and items — previously the reuse branch only fed
        them into the (transient) recommendation call and never wrote them
        back onto the TenderDocument, so a corrected date/code/item after
        first save silently vanished."""
        r1 = seeded_client.post("/api/invite/save", json={
            "project_name": "封面回填测试",
            "project_code": "OLD-CODE",
            "tender_date": "2026-01-01",
            "deadline": "2026-01-15",
            "items": [{"name": "阀门 DN50", "category": "阀门"}],
        })
        assert r1.status_code == 200, r1.text
        tender_id = r1.json()["tender_id"]

        r2 = seeded_client.post("/api/invite/save", json={
            "tender_id": tender_id,
            "project_name": "封面回填测试-改名",
            "project_code": "NEW-CODE",
            "tender_date": "2026-02-01",
            "deadline": "2026-02-15",
            "items": [
                {"name": "阀门 DN50", "category": "阀门"},
                {"name": "阀门 DN80", "category": "阀门"},
            ],
        })
        assert r2.status_code == 200, r2.text
        assert r2.json()["tender_id"] == tender_id

        from apps.api.core.database import SessionLocal
        from apps.api.models import TenderDocument
        db = SessionLocal()
        try:
            tender = db.get(TenderDocument, tender_id)
            assert tender is not None
            assert tender.project_name == "封面回填测试-改名"
            assert tender.project_code == "NEW-CODE"
            assert tender.tender_date == "2026-02-01"
            assert tender.deadline == "2026-02-15"
            assert len(tender.items or []) == 2
        finally:
            db.close()


# ─── TestBrandRecommendation ────────────────────────────────────────────────
class TestBrandRecommendation:
    """Detailed assertions on brand recommendation semantics."""

    def test_approved_only(self, brand_client):
        """Non-approved brands must not appear in recommendations."""
        # Add an unapproved brand directly to DB
        from apps.api.core.database import SessionLocal
        db = SessionLocal()
        try:
            db.add(BrandTier(
                brand_name="测试未审核品牌",
                tier="国产",
                category="阀门",
                is_approved=False,
            ))
            db.commit()
        finally:
            db.close()

        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"name": "截止阀 DN50", "category": "阀门"}],
            "top_n": 20,
        })
        assert r.status_code == 200
        names = [rec["brand_name"] for rec in r.json()["recommendations"]]
        assert "测试未审核品牌" not in names

    def test_brands_with_quotes_have_prices(self, brand_client):
        """Brands seeded with quotes must have non-null price_median."""
        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"category": "阀门"}],
            "top_n": 10,
        })
        assert r.status_code == 200
        recs = {rec["brand_name"]: rec for rec in r.json()["recommendations"]}

        # KITZ: 20 quotes → has prices
        assert "KITZ" in recs
        assert recs["KITZ"]["sample_count"] == 20
        assert recs["KITZ"]["price_median"] is not None

        # 上海冠龙: 0 quotes → no prices
        assert "上海冠龙" in recs
        assert recs["上海冠龙"]["sample_count"] == 0
        assert recs["上海冠龙"]["price_median"] is None

    def test_sorted_by_composite_score(self, brand_client):
        """复合评分单调不增；有数据的国产可超越零样本合资。"""
        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"category": "阀门"}],
            "top_n": 10,
        })
        recs = r.json()["recommendations"]
        scores = [_brand_score(rec) for rec in recs]
        for i in range(len(scores) - 1):
            assert scores[i] >= scores[i + 1], (
                f"score[{i}] {recs[i]['brand_name']}={scores[i]:.4f} < "
                f"score[{i+1}] {recs[i+1]['brand_name']}={scores[i+1]:.4f}"
            )
        # 上海良工（国产 15 样本, score≈0.48）应排在上海冠龙（国产 0 样本, score=0.00）之前
        names = [r["brand_name"] for r in recs]
        if "上海良工" in names and "上海冠龙" in names:
            assert names.index("上海良工") < names.index("上海冠龙")

    def test_top_n_respected(self, brand_client):
        """top_n parameter caps the result list."""
        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"category": "阀门"}],
            "top_n": 2,
        })
        assert r.status_code == 200
        assert len(r.json()["recommendations"]) <= 2

    def test_data_sufficient_tag(self, brand_client):
        """Brands with ≥20 samples should carry '数据充足' tag."""
        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"category": "阀门"}],
            "top_n": 10,
        })
        recs = {rec["brand_name"]: rec for rec in r.json()["recommendations"]}
        assert "数据充足" in recs["KITZ"]["tags"]       # 20 samples
        assert "有参考价格" in recs["WATTS"]["tags"]    # 5 samples
        assert "数据充足" not in recs["上海冠龙"]["tags"]  # 0 samples

    def test_no_categories_returns_empty(self, brand_client):
        """Completely unrecognized item names → empty recommendations."""
        r = brand_client.post("/api/invite/recommend", json={
            "tender_items": [{"name": "完全不认识的东西XYZ"}],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["categories"] == []
        assert body["recommendations"] == []


# ─── TestBrandRecommendJingqiao ─────────────────────────────────────────────
class TestBrandRecommendJingqiao:
    """Validate brand recommendation using the 金桥 J9A-03 fixture (18 valve items)."""

    @pytest.fixture(autouse=True)
    def _fixture(self, brand_client):
        self.client = brand_client
        with open(FIXTURE_DIR / "live_jingqiao_tender_result.json", encoding="utf-8") as f:
            data = json.load(f)
        self.items = data["items"]
        self.brand_requirements = data.get("brand_requirements", [])

    def test_infers_valve_category(self):
        r = self.client.post("/api/invite/recommend", json={
            "tender_items": self.items,
            "top_n": 10,
        })
        assert r.status_code == 200
        assert "阀门" in r.json()["categories"]

    def test_kitz_in_top3(self):
        """KITZ (20 samples) must rank in the top 3 for the 阀门 category."""
        r = self.client.post("/api/invite/recommend", json={
            "tender_items": self.items,
            "top_n": 10,
        })
        top3_names = [rec["brand_name"] for rec in r.json()["recommendations"][:3]]
        assert "KITZ" in top3_names, (
            f"KITZ not in top-3; top-3 was {top3_names}"
        )

    def test_all_brand_requirements_returned(self):
        """All three required brands (KITZ/WATTS/BERMAD) should appear in results.

        Note: BERMAD is NOT seeded, so the assertion is relaxed to:
        all seeded brands with is_approved=True appear.
        """
        r = self.client.post("/api/invite/recommend", json={
            "tender_items": self.items,
            "top_n": 10,
        })
        names = {rec["brand_name"] for rec in r.json()["recommendations"]}
        # Only check seeded brands; BERMAD absent from seed = correctly absent
        for seeded in ("KITZ", "WATTS", "上海良工", "上海冠龙"):
            assert seeded in names, f"{seeded} missing from recommendations"

    def test_kitz_price_data_present(self):
        """KITZ (20 quotes seeded) must have price_median, p10, p90."""
        r = self.client.post("/api/invite/recommend", json={
            "tender_items": self.items,
            "top_n": 10,
        })
        recs = {rec["brand_name"]: rec for rec in r.json()["recommendations"]}
        kitz = recs["KITZ"]
        assert kitz["sample_count"] == 20
        assert kitz["price_median"] is not None
        assert kitz["price_p10"] is not None
        assert kitz["price_p90"] is not None
        assert kitz["price_p10"] <= kitz["price_median"] <= kitz["price_p90"]


# ─── TestBrandRecommendE2E ──────────────────────────────────────────────────
class TestSupplierInvitationEvidence:
    """The actionable invitation path must persist deterministic evidence."""

    def test_supplier_recommendation_and_saved_evidence(self, brand_client):
        from apps.api.core.database import SessionLocal
        from apps.api.models import Material, Quote, Supplier, TenderDocument

        db = SessionLocal()
        try:
            supplier = Supplier(name="Evidence Supplier", cooperation_score=80)
            material = Material(
                standard_name="Evidence Cable Tray",
                profession="电气",
                category="桥架",
                unit="m",
                ref_price_reasonable_low=100,
            )
            db.add_all([supplier, material])
            db.flush()
            db.add_all([
                Quote(
                    material_id=material.id, supplier_id=supplier.id,
                    unit_price=95, brand="Evidence Brand",
                ),
                # Excluded history must not be used as brand evidence.
                Quote(
                    material_id=material.id, supplier_id=supplier.id,
                    unit_price=90, brand="Excluded Brand",
                    bid_status="excluded_from_ref",
                ),
            ])
            db.commit()
        finally:
            db.close()

        items = [{"name": "桥架 300x100", "category": "桥架"}]
        rec = brand_client.post("/api/invite/recommend", json={
            "tender_items": items, "top_n": 5,
        })
        assert rec.status_code == 200, rec.text
        suppliers = rec.json()["supplier_recommendations"]
        evidence = next(item for item in suppliers if item["supplier_name"] == "Evidence Supplier")
        assert evidence["reason"]["history_count"] == 1
        assert "Evidence Brand" in evidence["reason"]["brands"]
        assert "Excluded Brand" not in evidence["reason"]["brands"]

        saved = brand_client.post("/api/invite/save", json={
            "project_name": "Evidence Pilot",
            "items": items,
            "supplier_ids": [evidence["supplier_id"]],
        })
        assert saved.status_code == 200, saved.text
        invitation = saved.json()["invitations"][0]
        assert invitation["rank"] == evidence["rank"]
        assert invitation["score"] == evidence["score"]

        db = SessionLocal()
        try:
            tender = db.get(TenderDocument, saved.json()["tender_id"])
            assert tender.recommendation_snapshot["selected_supplier_ids"] == [evidence["supplier_id"]]
            assert tender.invitations[0].reason["history_count"] == 1
        finally:
            db.close()


@pytest.mark.e2e
class TestBrandRecommendE2E:
    """Fresh E2E: upload the real 金桥 PDF, parse tender items, then brand-recommend.

    Requires DASHSCOPE_API_KEY. Skipped in CI unless the key is present.
    Uses the actual recognition pipeline (no mock); validates that the real
    LLM output produces 阀门 items that correctly map to 阀门 brand recommendations.
    """

    @pytest.fixture(autouse=True)
    def _check_key(self, seeded_client):
        from apps.api.core.config import settings as s
        if not getattr(s, "DASHSCOPE_API_KEY", None):
            pytest.skip("DASHSCOPE_API_KEY not configured")
        self.client = seeded_client

    def test_upload_pdf_and_recommend(self):
        tender_pdf = REPO_ROOT / "docs" / "test" / "金桥地体上盖招标文件.pdf"
        if not tender_pdf.exists():
            pytest.skip(f"Fixture PDF not found: {tender_pdf}")

        with open(tender_pdf, "rb") as fh:
            r = self.client.post(
                "/api/intake/upload",
                data={"type": "tender"},
                files={"file": (tender_pdf.name, fh, "application/pdf")},
            )
        assert r.status_code == 200, r.text
        job_id = r.json()["id"]

        # Poll until done (real pipeline — allow up to 120s)
        body = {}
        for _ in range(60):
            r2 = self.client.get(f"/api/intake/jobs/{job_id}")
            body = r2.json()
            if body["status"] == "done":
                break
            time.sleep(2)
        assert body["status"] == "done", f"Job still {body['status']} after 120 s"
        assert body["result"] and body["result"].get("items"), "No items in result"

        items = body["result"]["items"]
        r3 = self.client.post("/api/invite/recommend", json={
            "tender_items": items,
            "top_n": 10,
        })
        assert r3.status_code == 200, r3.text
        rec_body = r3.json()
        assert "阀门" in rec_body["categories"], (
            f"Expected 阀门 in categories; got {rec_body['categories']}"
        )
        assert len(rec_body["recommendations"]) > 0, "No brand recommendations returned"


# ─── TestInferCategories ────────────────────────────────────────────────────
class TestInferCategories:
    """Unit test of the category-inference helper (no DB, no server)."""

    def test_explicit_category(self):
        from apps.api.services.supplier.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "X", "category": "桥架"}])
        assert cats == ["桥架"]

    def test_name_keyword_match(self):
        from apps.api.services.supplier.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "桥架300×200 镀锌"}])
        assert cats == ["桥架"]

    def test_dedupe(self):
        from apps.api.services.supplier.supplier_recommend import infer_categories
        cats = infer_categories([
            {"name": "桥架A"},
            {"name": "桥架B"},
            {"name": "阀门 DN100"},
        ])
        assert cats == ["桥架", "阀门"]

    def test_unknown_ignored(self):
        from apps.api.services.supplier.supplier_recommend import infer_categories
        cats = infer_categories([{"name": "随便起的名字"}])
        assert cats == []
