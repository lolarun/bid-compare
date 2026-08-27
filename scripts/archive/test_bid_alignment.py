"""Test bid-alignment suggest endpoint with real quote data from DB.

Picks quotes from the 3 suppliers that bid on the same tender,
sends them to POST /api/analysis/bid-alignment/suggest, and
verifies the LLM returns meaningful alignment groups.
"""
import json
import sqlite3
import sys
import requests

DB = "data/mempas.db"
API = "http://localhost:8002"


def get_token():
    """Login and return JWT token."""
    resp = requests.post(f"{API}/api/auth/login", json={"username": "admin", "password": "admin123"})
    if resp.status_code != 200:
        print(f"Login failed: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    return resp.json()["access_token"]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # Find a project that has quotes from multiple suppliers
    rows = conn.execute("""
        SELECT q.project_id, p.name AS project_name, COUNT(DISTINCT q.supplier_id) AS n_sup
        FROM quotes q
        JOIN projects p ON p.id = q.project_id
        GROUP BY q.project_id
        HAVING n_sup >= 2
        ORDER BY n_sup DESC
        LIMIT 1
    """).fetchall()

    if not rows:
        print("ERROR: No project with multiple supplier quotes found")
        sys.exit(1)

    project_id = rows[0]["project_id"]
    project_name = rows[0]["project_name"]
    n_sup = rows[0]["n_sup"]
    print(f"Project: {project_name} (id={project_id}, {n_sup} suppliers)")

    # Get quotes with supplier/material info
    quotes = conn.execute("""
        SELECT q.id AS quote_id, q.supplier_id, s.name AS supplier_name,
               m.standard_name AS material_name, m.spec, m.unit,
               q.quantity, q.unit_price, q.total_price,
               m.category
        FROM quotes q
        JOIN suppliers s ON s.id = q.supplier_id
        JOIN materials m ON m.id = q.material_id
        WHERE q.project_id = ?
        ORDER BY m.category, m.standard_name, s.name
    """, (project_id,)).fetchall()

    conn.close()

    print(f"Total quotes: {len(quotes)}")

    # Pick the most common category
    cat_counts = {}
    for q in quotes:
        c = q["category"] or "未分类"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    top_cat = max(cat_counts, key=cat_counts.get)
    print(f"Top category: {top_cat} ({cat_counts[top_cat]} quotes)")

    # Filter to top category, limit to 20 rows for speed (LLM can be slow)
    cat_quotes = [q for q in quotes if (q["category"] or "未分类") == top_cat][:20]
    supplier_ids = list(set(q["supplier_id"] for q in cat_quotes))

    payload = {
        "project_id": project_id,
        "category": top_cat,
        "supplier_ids": supplier_ids,
        "rows": [
            {
                "quote_id": q["quote_id"],
                "supplier_id": q["supplier_id"],
                "supplier_name": q["supplier_name"],
                "material_name": q["material_name"],
                "spec": q["spec"] or "",
                "unit": q["unit"] or "",
                "quantity": q["quantity"],
                "unit_price": q["unit_price"],
                "total_price": q["total_price"],
            }
            for q in cat_quotes
        ],
    }

    print(f"\nSending {len(payload['rows'])} rows to bid-alignment/suggest ...")
    print(f"Suppliers: {supplier_ids}")

    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.post(
        f"{API}/api/analysis/bid-alignment/suggest",
        json=payload,
        headers=headers,
        timeout=180,
    )

    print(f"\nHTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        sys.exit(1)

    data = resp.json()

    if data.get("error"):
        print(f"ERROR from LLM: {data['error']}")
        sys.exit(1)

    print(f"tokens_used: {data.get('tokens_used', 0)}")
    print(f"duration_ms: {data.get('duration_ms', 0)}")
    print(f"groups: {len(data.get('groups', []))}")
    print(f"field_fixes: {len(data.get('field_fixes', []))}")

    # Show first 3 groups
    for i, g in enumerate(data.get("groups", [])[:3]):
        print(f"\n--- Group {i+1} ---")
        print(f"  suggested_name: {g['suggested_name']}")
        print(f"  suggested_spec: {g['suggested_spec']}")
        print(f"  confidence: {g['confidence']}")
        print(f"  reason: {g['reason'][:80]}")
        print(f"  items: {len(g['items'])} quotes")
        for item in g["items"][:3]:
            print(f"    - quote_id={item['quote_id']} supplier_id={item['supplier_id']} action={item.get('action','')}")

    # Show field fixes
    for i, f in enumerate(data.get("field_fixes", [])[:5]):
        print(f"\n--- Fix {i+1} ---")
        print(f"  quote_id: {f['quote_id']}")
        print(f"  field: {f['field']}  current={f.get('current')} -> suggested={f.get('suggested')}")
        print(f"  confidence: {f['confidence']}")
        print(f"  reason: {f['reason'][:80]}")

    # Validate structure
    ok = True
    for g in data.get("groups", []):
        if not g.get("suggested_name"):
            print("FAIL: group missing suggested_name")
            ok = False
        if not g.get("items"):
            print("FAIL: group has no items")
            ok = False
        for item in g.get("items", []):
            if "quote_id" not in item or "supplier_id" not in item:
                print(f"FAIL: item missing quote_id/supplier_id: {item}")
                ok = False

    for f in data.get("field_fixes", []):
        if "quote_id" not in f:
            print("FAIL: fix missing quote_id")
            ok = False

    if ok and (data.get("groups") or data.get("field_fixes")):
        print("\nPASS: PASS — bid-alignment/suggest returned valid structured data")
    elif ok and not data.get("groups") and not data.get("field_fixes"):
        print("\nWARN:  WARN — No groups or fixes returned (data may be too clean)")
    else:
        print("\nFAIL: FAIL — structural validation errors")
        sys.exit(1)

    # Save full response for inspection
    with open("scripts/test_bid_alignment_result.json", "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    print("Full response saved to scripts/test_bid_alignment_result.json")


if __name__ == "__main__":
    main()
