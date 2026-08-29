"""Tests for API endpoints."""


# ─── Health ──────────────────────────────────────────────────────────────────

def test_health(legacy_client):
    resp = legacy_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ─── Materials CRUD ──────────────────────────────────────────────────────────

def test_create_material(legacy_client):
    resp = legacy_client.post("/api/materials", json={
        "standard_name": "蝶阀DN100",
        "profession": "给排水",
        "category": "阀门",
        "sub_category": "蝶阀",
        "spec": "DN100",
        "unit": "个",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["standard_name"] == "蝶阀DN100"
    assert data["material_code"].startswith("WS-VLV-")


def test_list_materials(legacy_client, sample_material):
    resp = legacy_client.get("/api/materials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1


def test_list_materials_filter_category(legacy_client, sample_material):
    resp = legacy_client.get("/api/materials", params={"category": "桥架"})
    data = resp.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["category"] == "桥架"


def test_list_materials_filter_empty(legacy_client, sample_material):
    resp = legacy_client.get("/api/materials", params={"category": "不存在的品类"})
    data = resp.json()
    assert data["total"] == 0


def test_get_material(legacy_client, sample_material):
    resp = legacy_client.get(f"/api/materials/{sample_material.id}")
    assert resp.status_code == 200
    assert resp.json()["material_code"] == "EL-BRG-00001"


def test_get_material_404(legacy_client):
    resp = legacy_client.get("/api/materials/99999")
    assert resp.status_code == 404


def test_update_material(legacy_client, sample_material):
    resp = legacy_client.put(f"/api/materials/{sample_material.id}", json={
        "spec": "400×200",
    })
    assert resp.status_code == 200
    assert resp.json()["spec"] == "400×200"


def test_disable_material_hides_from_default_list(legacy_client, sample_material):
    resp = legacy_client.post(f"/api/materials/{sample_material.id}/disable")
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"

    resp2 = legacy_client.get("/api/materials")
    assert resp2.status_code == 200
    ids = [item["id"] for item in resp2.json()["items"]]
    assert sample_material.id not in ids

    resp3 = legacy_client.get("/api/materials", params={"include_disabled": True})
    assert resp3.status_code == 200
    ids = [item["id"] for item in resp3.json()["items"]]
    assert sample_material.id in ids


def test_delete_material(legacy_client, sample_material):
    resp = legacy_client.delete(f"/api/materials/{sample_material.id}")
    assert resp.status_code == 204

    resp2 = legacy_client.get(f"/api/materials/{sample_material.id}")
    assert resp2.status_code == 404


def test_list_categories(legacy_client, sample_material):
    resp = legacy_client.get("/api/materials/categories")
    assert resp.status_code == 200
    cats = resp.json()
    assert any(c["category"] == "桥架" for c in cats)


# ─── Suppliers CRUD ──────────────────────────────────────────────────────────

def test_create_supplier(legacy_client):
    resp = legacy_client.post("/api/suppliers", json={
        "name": "新供应商B",
        "categories": ["阀门"],
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "新供应商B"


def test_create_supplier_duplicate(legacy_client, sample_supplier):
    resp = legacy_client.post("/api/suppliers", json={
        "name": sample_supplier.name,
    })
    assert resp.status_code == 409


def test_list_suppliers(legacy_client, sample_supplier):
    resp = legacy_client.get("/api/suppliers")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


def test_update_supplier(legacy_client, sample_supplier):
    resp = legacy_client.put(f"/api/suppliers/{sample_supplier.id}", json={
        "short_name": "新简称",
    })
    assert resp.status_code == 200
    assert resp.json()["short_name"] == "新简称"


# ─── Projects CRUD ───────────────────────────────────────────────────────────

def test_create_project(legacy_client):
    resp = legacy_client.post("/api/projects", json={
        "name": "测试项目二期",
        "code": "P2025-002",
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "测试项目二期"


def test_list_projects(legacy_client, sample_project):
    resp = legacy_client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ─── Quotes CRUD ─────────────────────────────────────────────────────────────

def test_create_quote(legacy_client, sample_material, sample_supplier):
    resp = legacy_client.post("/api/quotes", json={
        "material_id": sample_material.id,
        "supplier_id": sample_supplier.id,
        "unit_price": 48.0,
        "quantity": 100.0,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["unit_price"] == 48.0
    # Should auto-compute deviation vs baseline (reasonable_low or median=50)
    assert data["deviation_pct"] is not None
    assert data["alert_level"] in ("normal", "yellow", "red")


def test_list_quotes(legacy_client, sample_quotes):
    resp = legacy_client.get("/api/quotes")
    assert resp.status_code == 200
    assert resp.json()["total"] == 8


def test_list_quotes_filter_material(legacy_client, sample_material, sample_quotes):
    resp = legacy_client.get("/api/quotes", params={"material_id": sample_material.id})
    data = resp.json()
    assert data["total"] == 8


# ─── Analysis ────────────────────────────────────────────────────────────────

def test_price_compare(legacy_client, sample_material, sample_quotes):
    resp = legacy_client.post("/api/analysis/compare", json={
        "category": "桥架",
        "new_price": 55.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["sample_count"] == 8
    assert data["alert_level"] in ("normal", "yellow", "red")


def test_supplier_score(legacy_client, sample_supplier, sample_material, sample_quotes):
    resp = legacy_client.post("/api/analysis/supplier-score", json={
        "supplier_id": sample_supplier.id,
        "category": "桥架",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_name"] == "测试供应商A"
    assert 0 <= data["total_score"] <= 100


def test_dashboard(legacy_client, sample_material, sample_quotes):
    resp = legacy_client.get("/api/analysis/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_materials"] >= 1
    assert data["total_quotes"] >= 1
    assert len(data["category_stats"]) >= 1


# ─── Config ──────────────────────────────────────────────────────────────────

def test_list_configs(legacy_client):
    resp = legacy_client.get("/api/config")
    assert resp.status_code == 200
    configs = resp.json()
    keys = [c["key"] for c in configs]
    assert "scoring_weights" in keys
    assert "thresholds" in keys


def test_update_config(legacy_client):
    resp = legacy_client.put("/api/config/scoring_weights", json={
        "value": {
            "price":        0.50,
            "history":      0.15,
            "completeness": 0.15,
            "brand":        0.10,
            "commercial":   0.10,
        },
    })
    assert resp.status_code == 200
    assert resp.json()["value"]["price"] == 0.50
