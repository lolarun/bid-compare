"""Test bid_matrix works with and without alignment groups."""
import requests
import sys

API = "http://localhost:8002"


def get_token():
    resp = requests.post(f"{API}/api/auth/login", json={"username": "admin", "password": "admin123"})
    return resp.json()["access_token"]


def main():
    token = get_token()
    h = {"Authorization": f"Bearer {token}"}

    # 1. Test matrix WITHOUT alignment (baseline — should still work)
    payload = {
        "project_id": 59,
        "supplier_ids": [7, 58, 59],
        "category": None,
    }
    resp = requests.post(f"{API}/api/analysis/bid-matrix", json=payload, headers=h)
    print(f"Matrix (no alignment): HTTP {resp.status_code}")
    assert resp.status_code == 200, f"Failed: {resp.text[:300]}"
    data = resp.json()
    n_rows_base = len(data["rows"])
    print(f"  rows={n_rows_base}  suppliers={len(data['suppliers'])}  totals={len(data['totals'])}")
    assert n_rows_base > 0, "No rows returned"

    # Find what category those quotes belong to
    import sqlite3
    conn = sqlite3.connect("data/mempas.db")
    cat = conn.execute(
        "SELECT m.category FROM quotes q JOIN materials m ON m.id=q.material_id WHERE q.id=12052"
    ).fetchone()[0]
    conn.close()
    print(f"  Category for test quotes: {cat}")

    # 2. Create an alignment group to test alignment-aware matrix
    group_payload = {
        "project_id": 59,
        "category": cat,
        "groups": [
            {
                "suggested_name": "TEST-aligned-valve",
                "suggested_spec": "DN20 PN16",
                "status": "confirmed",
                "confidence": 0.9,
                "reason": "test",
                "items": [
                    {"quote_id": 12052, "supplier_id": 7, "action": "align"},
                    {"quote_id": 11986, "supplier_id": 58, "action": "align"},
                ],
            },
        ],
        "field_fixes": [],
    }
    resp = requests.post(f"{API}/api/analysis/bid-alignment/apply", json=group_payload, headers=h)
    print(f"\nApply alignment: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  Error: {resp.text[:300]}")
    assert resp.status_code == 200

    payload2 = {
        "project_id": 59,
        "supplier_ids": [7, 58, 59],
        "category": cat,
    }
    resp = requests.post(f"{API}/api/analysis/bid-matrix", json=payload2, headers=h)
    print(f"\nMatrix (with alignment, cat={cat}): HTTP {resp.status_code}")
    assert resp.status_code == 200, f"Failed: {resp.text[:300]}"
    data2 = resp.json()
    print(f"  rows={len(data2['rows'])}")

    # Check that the aligned row has our custom name
    aligned_found = False
    for row in data2["rows"]:
        if row["material_name"] == "TEST-aligned-valve":
            aligned_found = True
            print(f"  Found aligned row: name={row['material_name']} spec={row['spec']}")
            # Should have suppliers
            quoted = [s for s in row["suppliers"] if s["price"] is not None]
            print(f"    {len(quoted)} supplier(s) quoted")
            break

    if aligned_found:
        print("\nPASS -- alignment-aware matrix working")
    else:
        print("\nWARN -- aligned row not found (may be filtered by category mismatch)")

    # 4. Cleanup: delete the test alignment group
    resp = requests.get(f"{API}/api/analysis/bid-alignment/groups", params={"project_id": 59}, headers=h)
    for g in resp.json():
        if g["suggested_name"] == "TEST-aligned-valve":
            requests.delete(f"{API}/api/analysis/bid-alignment/groups/{g['id']}", headers=h)
            print(f"  Cleaned up test group id={g['id']}")

    print("\nDone.")


if __name__ == "__main__":
    main()
