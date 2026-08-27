"""Test bid_status import feature."""
import requests
import sqlite3

# Login
resp = requests.post("http://localhost:8002/api/auth/login", json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Upload Excel with bid_status=未中标
with open("docs/test/test_qiaojia_quotes.xlsx", "rb") as f:
    resp = requests.post(
        "http://localhost:8002/api/quotes/import",
        headers=headers,
        files={"file": ("test_qiaojia_quotes.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"category": "桥架", "bid_status": "未中标"},
    )

print(f"Import status: {resp.status_code}")
result = resp.json()
print(f"  imported: {result.get('imported')}, batch_id: {result.get('batch_id')}")

# Verify in DB
conn = sqlite3.connect("data/mempas.db")
batch_id = result.get("batch_id", "")
rows = conn.execute(
    "SELECT id, bid_status FROM quotes WHERE batch_id = ?", (batch_id,)
).fetchall()
print(f"\nDB verification (batch {batch_id}):")
for row in rows[:5]:
    print(f"  Quote {row[0]}: bid_status='{row[1]}'")

all_match = all(r[1] == "未中标" for r in rows)
print(f"\nAll {len(rows)} quotes have bid_status='未中标': {all_match}")

# Also verify heatmap excludes them
resp2 = requests.get("http://localhost:8002/api/dashboard/heatmap", headers=headers)
heatmap = resp2.json()
print(f"\nHeatmap nodes: {len(heatmap.get('nodes', []))}")

conn.close()
