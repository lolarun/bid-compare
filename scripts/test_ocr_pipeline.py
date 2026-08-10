"""Test OCR pipeline by uploading a quote PDF via intake API."""
import requests
import json
import time

resp = requests.post("http://localhost:8002/api/auth/login",
                     json={"username": "admin", "password": "admin123"})
token = resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Use one of the bidder PDFs as a quote
pdf_path = r"docs\test\徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页.pdf"
print(f"Uploading: {pdf_path}")

with open(pdf_path, "rb") as f:
    resp = requests.post(
        "http://localhost:8002/api/intake/upload",
        headers=headers,
        files={"file": ("quote.pdf", f, "application/pdf")},
        data={"type": "quote"},
        timeout=120,
    )

data = resp.json()
job_id = data.get("id")
status = data.get("status")
print(f"Job: {job_id} status={status}")

# Poll
while status in ("pending", "running"):
    time.sleep(3)
    resp = requests.get(f"http://localhost:8002/api/intake/jobs/{job_id}", headers=headers)
    data = resp.json()
    status = data.get("status")
    stage = data.get("progress_stage", "")
    pct = data.get("progress_pct", 0)
    print(f"  polling: status={status} stage={stage} pct={pct}%")

if data.get("result"):
    result = data["result"]
    items = result.get("items", [])
    supplier = result.get("supplier_name", "")
    print(f"\nSupplier: {supplier}")
    print(f"Extracted {len(items)} quote items:")
    for i, it in enumerate(items[:10]):
        print(f"  [{i+1}] {it.get('material','')} | spec={it.get('spec','')} | brand={it.get('brand','')} | "
              f"unit_price={it.get('unit_price')} | qty={it.get('qty')} | total={it.get('total_price')}")
    if len(items) > 10:
        print(f"  ... and {len(items) - 10} more items")
    print(f"\nTokens: {data.get('tokens_used')} Duration: {data.get('duration_ms')}ms")
else:
    print(f"Error: {data.get('error')}")
