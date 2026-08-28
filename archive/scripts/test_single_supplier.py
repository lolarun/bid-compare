"""Test single-supplier comparison via bid-matrix API."""
import requests

# Login
resp = requests.post("http://localhost:8002/api/auth/login", json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Get supplier IDs
resp = requests.get("http://localhost:8002/api/suppliers?page=1&page_size=5", headers=headers)
suppliers = resp.json()["items"]
print(f"Found {len(suppliers)} suppliers:")
for s in suppliers[:5]:
    print(f"  {s['id']}: {s['name']}")

# Test with single supplier (first one)
if suppliers:
    sid = suppliers[0]["id"]
    print(f"\nTesting single-supplier bid-matrix with supplier_id={sid}, category=桥架")
    resp = requests.post(
        "http://localhost:8002/api/analysis/bid-matrix",
        headers=headers,
        json={"supplier_ids": [sid], "category": "桥架"},
    )
    if resp.status_code == 200:
        data = resp.json()
        print(f"  Status: OK")
        print(f"  Suppliers: {[s['name'] for s in data['suppliers']]}")
        print(f"  Rows: {len(data['rows'])}")
        print(f"  brand_tier_filter: {data.get('brand_tier_filter')}")
        for row in data["rows"][:3]:
            print(f"  - {row['material_name']} {row['spec']}: "
                  f"price={row['suppliers'][0]['price']}, "
                  f"deviation={row['suppliers'][0]['deviation_pct']}, "
                  f"baseline={row.get('reasonable_low', {})}")
        if data["totals"]:
            t = data["totals"][0]
            print(f"  Totals: total={t['total']}, avg_dev={t['avg_deviation']}, anomalies={t['anomaly_count']}")
    else:
        print(f"  Error: {resp.status_code} {resp.text}")
