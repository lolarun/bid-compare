"""Test tender extraction with extended_attrs."""
import requests
import json
import time

resp = requests.post("http://localhost:8002/api/auth/login",
                     json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

with open(r"docs\test\材料采购招标文件审批表.pdf", "rb") as f:
    resp = requests.post(
        "http://localhost:8002/api/intake/upload",
        headers=headers,
        files={"file": ("tender.pdf", f, "application/pdf")},
        data={"type": "tender"},
        timeout=120,
    )

data = resp.json()
print(f"Status: {resp.status_code}")
job_status = data.get("status")
job_id = data.get("id")
print(f"Job status: {job_status}, id: {job_id}")

# Poll if async
while job_status in ("pending", "running"):
    time.sleep(3)
    resp = requests.get(f"http://localhost:8002/api/intake/jobs/{job_id}", headers=headers)
    data = resp.json()
    job_status = data.get("status")
    print(f"  polling... status={job_status} progress={data.get('progress_stage')} {data.get('progress_pct')}%")

if data.get("result"):
    items = data["result"].get("items", [])
    print(f"\nExtracted {len(items)} items:")
    for it in items:
        ext = it.get("extended_attrs", {})
        ext_str = json.dumps(ext, ensure_ascii=False) if ext else "{}"
        print(f"  {it['name']} | cat={it.get('category','')} | spec={it.get('spec','')} | ext={ext_str}")
else:
    print(f"Error: {data.get('error')}")
