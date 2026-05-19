"""Tests for newly added API endpoints: standardize, extended-schema, import, quote stats,
multi-compare, category-stats, multi-baseline."""

import io
import pytest
from apps.api.models import Material, Supplier, Quote, Project


# ─── Standardize endpoint ───────────────────────────────────────────────────

class TestStandardizeAPI:
    def test_standardize_basic(self, client):
        resp = client.post("/api/materials/standardize", json={
            "text": "热镀锌桥架 300*150",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "热浸镀锌" in data["standardized"]
        assert "300×150" in data["standardized"]
        assert len(data["changes"]) >= 2

    def test_standardize_with_category(self, client):
        resp = client.post("/api/materials/standardize", json={
            "text": "蝶型阀 DN100",
            "category": "阀门",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "蝶阀" in data["standardized"]

    def test_standardize_no_changes(self, client):
        resp = client.post("/api/materials/standardize", json={
            "text": "DN100 托盘式桥架",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["changes"] == []


# ─── Extended schema endpoint ───────────────────────────────────────────────

class TestExtendedSchemaAPI:
    def test_get_schema(self, client):
        resp = client.get("/api/materials/extended-schema/桥架")
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "桥架"
        assert len(data["fields"]) == 4
        keys = [f["key"] for f in data["fields"]]
        assert "surface" in keys
        assert "thickness" in keys

    def test_all_categories_have_schema(self, client):
        categories = ["桥架", "母线槽", "配电箱", "阀门", "不锈钢管",
                       "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵"]
        for cat in categories:
            resp = client.get(f"/api/materials/extended-schema/{cat}")
            assert resp.status_code == 200, f"Missing schema for {cat}"
            data = resp.json()
            assert len(data["fields"]) >= 3

    def test_schema_field_roles(self, client):
        resp = client.get("/api/materials/extended-schema/阀门")
        data = resp.json()
        roles = {f["key"]: f["role"] for f in data["fields"]}
        assert roles["valve_type"] == "匹配"
        assert roles["body_material"] == "差异"

    def test_unknown_category_404(self, client):
        resp = client.get("/api/materials/extended-schema/不存在的品类")
        assert resp.status_code == 404


# ─── Import endpoint ────────────────────────────────────────────────────────

class TestImportAPI:
    def test_import_csv(self, client):
        csv = "名称,规格型号,品牌,单位,含税单价,数量\n桥架A,300×150,泰和,m,50,100\n"
        resp = client.post("/api/quotes/import", files={
            "file": ("test.csv", io.BytesIO(csv.encode("utf-8-sig")), "text/csv"),
        }, data={"category": "桥架", "project_name": "测试项目"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["imported"] == 1

    def test_import_no_file(self, client):
        resp = client.post("/api/quotes/import", data={"category": "桥架"})
        assert resp.status_code == 422  # missing required file

    def test_import_invalid_extension(self, client):
        resp = client.post("/api/quotes/import", files={
            "file": ("test.txt", io.BytesIO(b"data"), "text/plain"),
        }, data={"category": "桥架"})
        assert resp.status_code == 400

    def test_import_unknown_category(self, client):
        csv = "名称,含税单价\ntest,100\n"
        resp = client.post("/api/quotes/import", files={
            "file": ("test.csv", io.BytesIO(csv.encode()), "text/csv"),
        }, data={"category": "不存在品类"})
        assert resp.status_code == 422


# ─── Quote stats endpoint ───────────────────────────────────────────────────

class TestQuoteStatsAPI:
    def test_stats_empty(self, client):
        resp = client.get("/api/quotes/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0

    def test_stats_with_data(self, client, sample_quotes):
        resp = client.get("/api/quotes/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 8
        assert data["avg_price"] is not None
        assert data["min_price"] is not None
        assert data["max_price"] is not None

    def test_stats_filter_by_category(self, client, sample_quotes):
        resp = client.get("/api/quotes/stats?category=桥架")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 8

        resp = client.get("/api/quotes/stats?category=阀门")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ─── Multi-compare endpoint ─────────────────────────────────────────────────

class TestMultiCompareAPI:
    def test_multi_compare(self, client, db_session, sample_material):
        # Create two suppliers with quotes
        sup1 = Supplier(name="供应商比较A", categories=["桥架"])
        sup2 = Supplier(name="供应商比较B", categories=["桥架"])
        db_session.add_all([sup1, sup2])
        db_session.flush()

        for price, sup in [(50, sup1), (55, sup1), (48, sup2), (52, sup2)]:
            q = Quote(
                material_id=sample_material.id,
                supplier_id=sup.id,
                unit_price=price,
            )
            db_session.add(q)
        db_session.commit()

        resp = client.post("/api/analysis/multi-compare", json={
            "supplier_ids": [sup1.id, sup2.id],
            "category": "桥架",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "桥架"
        assert len(data["suppliers"]) == 2

    def test_multi_compare_nonexistent_supplier(self, client):
        resp = client.post("/api/analysis/multi-compare", json={
            "supplier_ids": [9999],
            "category": "桥架",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["suppliers"]) == 0


# ─── Category stats endpoint ────────────────────────────────────────────────

class TestCategoryStatsAPI:
    def test_category_stats(self, client, sample_quotes):
        resp = client.get("/api/analysis/category-stats/桥架")
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "桥架"
        assert data["total_records"] > 0
        assert data["valid_prices"] > 0
        assert len(data["sub_categories"]) >= 1

    def test_category_stats_subcat_fields(self, client, sample_quotes):
        resp = client.get("/api/analysis/category-stats/桥架")
        data = resp.json()
        sub = data["sub_categories"][0]
        assert "count" in sub
        assert "mean" in sub
        assert "median" in sub
        assert "cv" in sub
        assert "suggested_threshold" in sub

    def test_category_stats_no_data(self, client):
        resp = client.get("/api/analysis/category-stats/不存在品类")
        assert resp.status_code == 404


# ─── Multi-baseline price compare ───────────────────────────────────────────

class TestMultiBaselineAPI:
    def test_compare_with_median(self, client, sample_quotes):
        resp = client.post("/api/analysis/compare", json={
            "category": "桥架",
            "sub_category": "托盘式桥架",
            "new_price": 55.0,
            "baseline_type": "median",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["sample_count"] > 0
        assert data["deviation_pct"] is not None

    def test_compare_with_mean(self, client, sample_quotes):
        resp = client.post("/api/analysis/compare", json={
            "category": "桥架",
            "sub_category": "托盘式桥架",
            "new_price": 55.0,
            "baseline_type": "mean",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["deviation_pct"] is not None

    def test_compare_with_lowest(self, client, sample_quotes):
        resp = client.post("/api/analysis/compare", json={
            "category": "桥架",
            "new_price": 55.0,
            "baseline_type": "lowest",
        })
        assert resp.status_code == 200
        data = resp.json()
        # Lowest baseline means higher deviation for a 55 price
        assert data["deviation_pct"] is not None
        assert data["deviation_pct"] > 0

    def test_compare_no_data(self, client):
        resp = client.post("/api/analysis/compare", json={
            "category": "不存在品类",
            "new_price": 100.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["sample_count"] == 0
        assert data["deviation_pct"] is None
