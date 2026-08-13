"""Test brand requirements in invite recommend."""
import requests
import json

resp = requests.post("http://localhost:8002/api/auth/login", json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Test without brand requirements
resp = requests.post(
    "http://localhost:8002/api/invite/recommend",
    headers=headers,
    json={
        "tender_items": [{"name": "桥架", "category": "桥架"}],
        "top_n": 3,
    },
)
print(f"Without brands - status: {resp.status_code}")
data = resp.json()
for r in data["recommendations"]:
    brands = r["reason"].get("brands", [])
    print(f"  #{r['rank']} {r['supplier_name']} (score={r['score']}) brands={brands[:5]}")

# Test with brand requirements
print()
resp = requests.post(
    "http://localhost:8002/api/invite/recommend",
    headers=headers,
    json={
        "tender_items": [{"name": "桥架", "category": "桥架"}],
        "top_n": 3,
        "brand_requirements": ["日立"],
    },
)
print(f"With brand_requirements=['日立'] - status: {resp.status_code}")
data = resp.json()
for r in data["recommendations"]:
    brands = r["reason"].get("brands", [])
    print(f"  #{r['rank']} {r['supplier_name']} (score={r['score']}) brands={brands[:5]}")
