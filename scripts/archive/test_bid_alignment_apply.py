"""Test bid-alignment apply endpoint — persist groups and field fixes."""
import json
import requests
import sys

API = "http://localhost:8002"


def get_token():
    resp = requests.post(f"{API}/api/auth/login", json={"username": "admin", "password": "admin123"})
    if resp.status_code != 200:
        print(f"Login failed: {resp.status_code}")
        sys.exit(1)
    return resp.json()["access_token"]


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Apply with sample groups
    payload = {
        "project_id": 59,
        "category": "test_alignment",
        "groups": [
            {
                "suggested_name": "Y-filter DN20",
                "suggested_spec": "DN20, PN16",
                "suggested_unit": "pcs",
                "suggested_qty": 1.0,
                "confidence": 0.95,
                "reason": "test group 1",
                "status": "confirmed",
                "items": [
                    {"quote_id": 12052, "supplier_id": 7, "action": "align"},
                    {"quote_id": 11986, "supplier_id": 58, "action": "align"},
                ],
            },
            {
                "suggested_name": "Y-filter DN25",
                "suggested_spec": "DN25, PN16",
                "status": "rejected",  # should be skipped
                "items": [
                    {"quote_id": 12053, "supplier_id": 7, "action": "align"},
                ],
            },
        ],
        "field_fixes": [],
    }

    resp = requests.post(f"{API}/api/analysis/bid-alignment/apply", json=payload, headers=headers)
    print(f"Apply: HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        sys.exit(1)

    data = resp.json()
    print(f"  groups_saved={data['groups_saved']}  items_saved={data['items_saved']}  fixes_applied={data['fixes_applied']}")

    assert data["groups_saved"] == 1, f"Expected 1 group saved, got {data['groups_saved']}"
    assert data["items_saved"] == 2, f"Expected 2 items saved, got {data['items_saved']}"

    # 2. List groups
    resp = requests.get(f"{API}/api/analysis/bid-alignment/groups", params={"project_id": 59}, headers=headers)
    print(f"\nList groups: HTTP {resp.status_code}")
    groups = resp.json()
    print(f"  Found {len(groups)} group(s)")
    assert len(groups) >= 1

    group = groups[-1]  # our just-created group
    print(f"  Group: id={group['id']} name={group['suggested_name']} items={len(group['items'])}")
    assert group["suggested_name"] == "Y-filter DN20"
    assert len(group["items"]) == 2

    # 3. Delete group
    gid = group["id"]
    resp = requests.delete(f"{API}/api/analysis/bid-alignment/groups/{gid}", headers=headers)
    print(f"\nDelete group {gid}: HTTP {resp.status_code}")
    assert resp.status_code == 200

    # Verify deleted
    resp = requests.get(f"{API}/api/analysis/bid-alignment/groups", params={"project_id": 59, "category": "test_alignment"}, headers=headers)
    groups = resp.json()
    print(f"  After delete: {len(groups)} group(s)")
    assert len(groups) == 0

    print("\nPASS -- bid-alignment apply/list/delete all working")


if __name__ == "__main__":
    main()
