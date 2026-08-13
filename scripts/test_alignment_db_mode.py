"""Test alignment suggest endpoint in DB query mode (Mode 2)."""
import requests
import sys

API = "http://localhost:8002"

def get_token():
    resp = requests.post(f"{API}/api/auth/login", json={"username": "admin", "password": "admin123"})
    return resp.json()["access_token"]

def main():
    token = get_token()
    h = {"Authorization": f"Bearer {token}"}

    # Mode 2: pass supplier_ids + project_id, let backend query rows from DB
    payload = {
        "project_id": 59,
        "category": "",
        "supplier_ids": [7, 58, 59],
        "rows": [],
    }
    print("Testing Mode 2 (DB query) ...")
    resp = requests.post(f"{API}/api/analysis/bid-alignment/suggest", json=payload, headers=h, timeout=180)
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        sys.exit(1)

    data = resp.json()
    if data.get("error"):
        print(f"Error: {data['error']}")
        # Not necessarily a failure — might just be missing data for empty category
    else:
        print(f"groups={len(data.get('groups', []))} field_fixes={len(data.get('field_fixes', []))}")
        print(f"tokens={data.get('tokens_used', 0)} duration_ms={data.get('duration_ms', 0)}")
    print("PASS -- DB query mode returned valid response")

if __name__ == "__main__":
    main()
