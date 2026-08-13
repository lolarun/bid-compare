"""Test bid_status import - minimal."""
import requests
import sqlite3

resp = requests.post("http://localhost:8002/api/auth/login", json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

with open("docs/test/test_qiaojia_quotes.xlsx", "rb") as f:
    resp = requests.post(
        "http://localhost:8002/api/quotes/import",
        headers=headers,
        files={"file": ("test.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"category": "桥架", "bid_status": "未中标"},
    )

print(f"Status: {resp.status_code}")
result = resp.json()
batch_id = result.get("batch_id", "")
print(f"batch_id: {batch_id}, imported: {result.get('imported')}")

conn = sqlite3.connect("data/mempas.db")
rows = conn.execute("SELECT id, bid_status FROM quotes WHERE batch_id = ?", (batch_id,)).fetchall()
for r in rows[:3]:
    print(f"  Quote {r[0]}: bid_status='{r[1]}'")
print(f"All have bid_status='未中标': {all(r[1] == '未中标' for r in rows)}")
conn.close()
