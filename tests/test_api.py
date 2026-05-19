"""Tests for API endpoints."""


# ─── Health ──────────────────────────────────────────────────────────────────

def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ─── Materials CRUD ──────────────────────────────────────────────────────────

def test_create_material(client):
    resp = client.post("/api/materials", json={
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


def test_list_materials(client, sample_material):
    resp = client.get("/api/materials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1


def test_list_materials_filter_category(client, sample_material):
    resp = client.get("/api/materials", params={"category": "桥架"})
    data = resp.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["category"] == "桥架"


def test_list_materials_filter_empty(client, sample_material):
    resp = client.get("/api/materials", params={"category": "不存在的品类"})
    data = resp.json()
    assert data["total"] == 0


def test_get_material(client, sample_material):
    resp = client.get(f"/api/materials/{sample_material.id}")
    assert resp.status_code == 200
    assert resp.json()["material_code"] == "EL-BRG-00001"


def test_get_material_404(client):
    resp = client.get("/api/materials/99999")
    assert resp.status_code == 404


def test_update_material(client, sample_material):
    resp = client.put(f"/api/materials/{sample_material.id}", json={
        "spec": "400×200",
    })
    assert resp.status_code == 200
    assert resp.json()["spec"] == "400×200"


def test_delete_material(client, sample_material):
    resp = client.delete(f"/api/materials/{sample_material.id}")
    assert resp.status_code == 204

    resp2 = client.get(f"/api/materials/{sample_material.id}")
    assert resp2.status_code == 404


def test_list_categories(client, sample_material):
    resp = client.get("/api/materials/categories")
    assert resp.status_code == 200
    cats = resp.json()
    assert any(c["category"] == "桥架" for c in cats)


# ─── Suppliers CRUD ──────────────────────────────────────────────────────────

def test_create_supplier(client):
    resp = client.post("/api/suppliers", json={
        "name": "新供应商B",
        "categories": ["阀门"],
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "新供应商B"


def test_create_supplier_duplicate(client, sample_supplier):
    resp = client.post("/api/suppliers", json={
        "name": sample_supplier.name,
    })
    assert resp.status_code == 409


def test_list_suppliers(client, sample_supplier):
    resp = client.get("/api/suppliers")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


def test_update_supplier(client, sample_supplier):
    resp = client.put(f"/api/suppliers/{sample_supplier.id}", json={
        "short_name": "新简称",
    })
    assert resp.status_code == 200
    assert resp.json()["short_name"] == "新简称"


# ─── Projects CRUD ───────────────────────────────────────────────────────────

def test_create_project(client):
    resp = client.post("/api/projects", json={
        "name": "测试项目二期",
        "code": "P2025-002",
    })
    assert resp.status_code == 201
    assert resp.json()["name"] == "测试项目二期"


def test_list_projects(client, sample_project):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1


# ─── Quotes CRUD ─────────────────────────────────────────────────────────────

def test_create_quote(client, sample_material, sample_supplier):
    resp = client.post("/api/quotes", json={
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


def test_list_quotes(client, sample_quotes):
    resp = client.get("/api/quotes")
    assert resp.status_code == 200
    assert resp.json()["total"] == 8


def test_list_quotes_filter_material(client, sample_material, sample_quotes):
    resp = client.get("/api/quotes", params={"material_id": sample_material.id})
    data = resp.json()
    assert data["total"] == 8


# ─── Analysis ────────────────────────────────────────────────────────────────

def test_price_compare(client, sample_material, sample_quotes):
    resp = client.post("/api/analysis/compare", json={
        "category": "桥架",
        "new_price": 55.0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["sample_count"] == 8
    assert data["alert_level"] in ("normal", "yellow", "red")


def test_supplier_score(client, sample_supplier, sample_material, sample_quotes):
    resp = client.post("/api/analysis/supplier-score", json={
        "supplier_id": sample_supplier.id,
        "category": "桥架",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["supplier_name"] == "测试供应商A"
    assert 0 <= data["total_score"] <= 100


def test_dashboard(client, sample_material, sample_quotes):
    resp = client.get("/api/analysis/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_materials"] >= 1
    assert data["total_quotes"] >= 1
    assert len(data["category_stats"]) >= 1


# ─── Config ──────────────────────────────────────────────────────────────────

def test_list_configs(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    configs = resp.json()
    keys = [c["key"] for c in configs]
    assert "scoring_weights" in keys
    assert "thresholds" in keys


def test_update_config(client):
    resp = client.put("/api/config/scoring_weights", json={
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
