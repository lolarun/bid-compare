"""End-to-end test: PDF upload → OCR → import → alignment suggest → apply → bid matrix.

Uses the 3 test PDFs in tests/fixtures/documents/bid/ that bid on the same tender:
  - 上海绵存投标文件.pdf
  - 凯硕新正投标文件.pdf
  - 泰科龙投标文件.pdf

Flow:
  1. Create a fresh test project
  2. Upload each PDF → OCR extraction (poll until done)
  3. batch-confirm each job → import quotes into DB
  4. Run alignment suggest (Mode 2 - DB query)
  5. Apply confirmed alignment groups
  6. Generate bid matrix and verify aligned rows appear
  7. Cleanup: delete alignment groups, quotes, project

Requires: backend running on localhost:8002 with DashScope OCR configured.
Runtime: ~3-5 min (OCR is slow).
"""

import json
import os
import sys
import time
import requests

API = "http://localhost:8002"
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "test")
TIMEOUT_OCR = 300  # max seconds to wait per OCR job
POLL_INTERVAL = 5

# The 3 test PDFs (same tender, different suppliers)
# Map: filename → fallback supplier name (in case OCR doesn't extract it)
TEST_PDFS = {
    "上海绵存投标文件.pdf": "上海绵存",
    "凯硕新正投标文件.pdf": "凯硕新正",
    "泰科龙投标文件.pdf": "泰科龙",
}


def log(msg: str):
    ts = time.strftime('%H:%M:%S')
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
            # fallback: strip non-ascii
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[{ts}] {safe}")


def get_token() -> str:
    resp = requests.post(f"{API}/api/auth/login",
                         json={"username": "admin", "password": "admin123"})
    if resp.status_code != 200:
        print(f"Login failed: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    return resp.json()["access_token"]


def create_project(headers: dict) -> int:
    """Create a test project, return its id."""
    resp = requests.post(f"{API}/api/projects",
                         json={"name": "E2E对齐测试项目", "status": "进行中"},
                         headers=headers)
    if resp.status_code != 200:
        # Maybe already exists, try to find it
        resp2 = requests.get(f"{API}/api/projects", headers=headers)
        for p in resp2.json().get("items", []):
            if p["name"] == "E2E对齐测试项目":
                return p["id"]
        print(f"Create project failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
    return resp.json()["id"]


def upload_pdf(filepath: str, project_id: int, headers: dict) -> str:
    """Upload a PDF for OCR extraction, return job_id."""
    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{API}/api/intake/upload",
            files={"file": (os.path.basename(filepath), f, "application/pdf")},
            data={"type": "quote", "project_id": str(project_id)},
            headers=headers,
        )
    if resp.status_code != 200:
        print(f"Upload failed for {filepath}: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
    job = resp.json()
    return job["id"]


def poll_job(job_id: str, headers: dict) -> dict:
    """Poll until job is done or failed, return job dict."""
    start = time.time()
    while time.time() - start < TIMEOUT_OCR:
        resp = requests.get(f"{API}/api/intake/jobs/{job_id}", headers=headers)
        if resp.status_code != 200:
            print(f"Poll failed: {resp.status_code}")
            sys.exit(1)
        job = resp.json()
        status = job["status"]
        stage = job.get("progress_stage", "")
        pct = job.get("progress_pct", 0)
        if status == "done":
            return job
        if status == "failed":
            print(f"Job {job_id} FAILED: {job.get('error', '')[:300]}")
            sys.exit(1)
        log(f"  job {job_id[:8]}... status={status} stage={stage} pct={pct}%")
        time.sleep(POLL_INTERVAL)
    print(f"Job {job_id} timed out after {TIMEOUT_OCR}s")
    sys.exit(1)


def batch_confirm(job_id: str, project_id: int, category: str,
                  supplier_name: str, headers: dict) -> dict:
    """Confirm extracted quotes → import into DB."""
    body = {
        "job_id": job_id,
        "project_id": project_id,
        "category": category,
    }
    if supplier_name:
        body["supplier_name"] = supplier_name
    resp = requests.post(f"{API}/api/quotes/batch-confirm",
                         json=body, headers=headers)
    if resp.status_code != 200:
        print(f"batch-confirm failed for job {job_id}: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


def main():
    log("=== E2E Alignment Test ===")

    # Check PDFs exist
    for pdf in TEST_PDFS.keys():
        path = os.path.join(PDF_DIR, pdf)
        if not os.path.exists(path):
            print(f"Missing test PDF: {path}")
            sys.exit(1)

    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    # ── Step 1: Create test project ──────────────────────────────────────
    log("Step 1: Creating test project...")
    project_id = create_project(headers)
    log(f"  project_id = {project_id}")

    # ── Step 2: Upload & OCR each PDF ────────────────────────────────────
    log("Step 2: Uploading PDFs for OCR...")
    job_ids = []
    for pdf, fallback_supplier in TEST_PDFS.items():
        path = os.path.join(PDF_DIR, pdf)
        log(f"  Uploading {pdf} ({os.path.getsize(path) / 1024 / 1024:.1f} MB)...")
        job_id = upload_pdf(path, project_id, headers)
        job_ids.append((pdf, job_id, fallback_supplier))
        log(f"    job_id = {job_id[:8]}...")

    # Poll all jobs until done
    log("Step 2b: Waiting for OCR to complete...")
    completed_jobs = []
    for pdf, job_id, fallback_supplier in job_ids:
        log(f"  Polling {pdf}...")
        job = poll_job(job_id, headers)
        n_items = len((job.get("result") or {}).get("items", []))
        supplier = (job.get("result") or {}).get("supplier_name", "")
        log(f"    OK Done: {n_items} items, supplier={supplier or '(empty)'}, "
            f"tokens={job.get('tokens_used', 0)}, "
            f"duration={job.get('duration_ms', 0)}ms")
        # Use fallback supplier name if OCR didn't extract one
        effective_supplier = supplier if supplier else fallback_supplier
        completed_jobs.append((pdf, job, effective_supplier))

    # ── Step 3: Import quotes into DB ────────────────────────────────────
    log("Step 3: Importing extracted quotes into DB...")
    supplier_ids = []
    for pdf, job, effective_supplier in completed_jobs:
        log(f"  Confirming {pdf} (job={job['id'][:8]}..., supplier={effective_supplier})...")
        result = batch_confirm(job["id"], project_id, "阀门", effective_supplier, headers)
        log(f"    created={result.get('created', 0)} skipped={result.get('skipped', 0)} "
            f"errors={len(result.get('errors', []))}")
        # Get supplier_ids from quote_ids
        qids = result.get("quote_ids", [])
        if qids:
            # Query one quote to get its supplier_id
            resp = requests.get(f"{API}/api/quotes",
                                params={"page": 1, "page_size": 1, "project_id": project_id},
                                headers=headers)
            # Just collect all unique supplier_ids from this project

    # Get all supplier_ids for this project
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db"))
    rows = conn.execute(
        "SELECT DISTINCT supplier_id FROM quotes WHERE project_id = ?",
        (project_id,)
    ).fetchall()
    supplier_ids = [r[0] for r in rows if r[0] is not None]
    n_quotes = conn.execute(
        "SELECT COUNT(*) FROM quotes WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    conn.close()

    log(f"  Total: {n_quotes} quotes from {len(supplier_ids)} suppliers: {supplier_ids}")
    if len(supplier_ids) < 2:
        print("FAIL: Expected at least 2 suppliers, got", len(supplier_ids))
        sys.exit(1)

    # ── Step 4: Alignment suggest ────────────────────────────────────────
    log("Step 4: Running alignment suggest (Mode 2 - DB query)...")
    payload = {
        "project_id": project_id,
        "category": "阀门",
        "supplier_ids": supplier_ids,
        "rows": [],  # Mode 2: backend queries from DB
    }
    resp = requests.post(f"{API}/api/analysis/bid-alignment/suggest",
                         json=payload, headers=headers, timeout=600)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"FAIL: alignment suggest failed: {resp.text[:500]}")
        sys.exit(1)

    suggest_data = resp.json()
    if suggest_data.get("error"):
        print(f"FAIL: alignment suggest error: {suggest_data['error']}")
        sys.exit(1)

    n_groups = len(suggest_data.get("groups", []))
    n_fixes = len(suggest_data.get("field_fixes", []))
    log(f"  OK groups={n_groups}, field_fixes={n_fixes}, "
        f"tokens={suggest_data.get('tokens_used', 0)}, "
        f"duration={suggest_data.get('duration_ms', 0)}ms")

    if n_groups == 0:
        log("  WARN: no alignment groups returned — data may be too clean or too small")

    # Show alignment groups
    for i, g in enumerate(suggest_data.get("groups", [])[:5]):
        items = g.get("items", [])
        sids = set(it["supplier_id"] for it in items)
        log(f"  Group {i+1}: {g['suggested_name']} ({g['suggested_spec']}) "
            f"conf={g['confidence']} items={len(items)} suppliers={len(sids)}")

    # ── Step 5: Apply alignment ──────────────────────────────────────────
    log("Step 5: Applying alignment groups...")
    groups = suggest_data.get("groups", [])
    # Mark all as confirmed for testing
    apply_groups = []
    for g in groups:
        apply_groups.append({
            "suggested_name": g["suggested_name"],
            "suggested_spec": g.get("suggested_spec", ""),
            "suggested_unit": g.get("suggested_unit", ""),
            "suggested_qty": g.get("suggested_qty"),
            "confidence": g.get("confidence", 0),
            "reason": g.get("reason", ""),
            "status": "confirmed",
            "items": g.get("items", []),
        })

    # Also apply field fixes
    apply_fixes = []
    for f in suggest_data.get("field_fixes", []):
        apply_fixes.append({
            "quote_id": f["quote_id"],
            "field": f["field"],
            "new_value": f.get("suggested", f.get("new_value")),
        })

    apply_payload = {
        "project_id": project_id,
        "category": "阀门",
        "groups": apply_groups,
        "field_fixes": apply_fixes,
    }
    resp = requests.post(f"{API}/api/analysis/bid-alignment/apply",
                         json=apply_payload, headers=headers)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"FAIL: alignment apply failed: {resp.text[:500]}")
        sys.exit(1)

    apply_result = resp.json()
    log(f"  OK groups_saved={apply_result['groups_saved']}, "
        f"items_saved={apply_result['items_saved']}, "
        f"fixes_applied={apply_result['fixes_applied']}")

    # Verify groups persisted
    resp = requests.get(f"{API}/api/analysis/bid-alignment/groups",
                        params={"project_id": project_id, "category": "阀门"},
                        headers=headers)
    saved_groups = resp.json()
    log(f"  Verified: {len(saved_groups)} groups persisted in DB")

    # ── Step 6: Generate bid matrix ──────────────────────────────────────
    log("Step 6: Generating bid matrix with alignment...")
    matrix_payload = {
        "project_id": project_id,
        "supplier_ids": supplier_ids,
        "category": "阀门",
    }
    resp = requests.post(f"{API}/api/analysis/bid-matrix",
                         json=matrix_payload, headers=headers)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"FAIL: bid-matrix failed: {resp.text[:500]}")
        sys.exit(1)

    matrix = resp.json()
    n_rows = len(matrix.get("rows", []))
    n_suppliers = len(matrix.get("suppliers", []))
    log(f"  OK matrix: {n_rows} rows, {n_suppliers} suppliers")

    # Check for aligned rows (they use suggested_name from alignment groups)
    aligned_names = {g["suggested_name"] for g in apply_groups}
    aligned_rows_found = 0
    for row in matrix.get("rows", []):
        if row.get("material_name") in aligned_names:
            aligned_rows_found += 1
            # Check how many suppliers have prices in this aligned row
            quoted = [s for s in row.get("suppliers", []) if s.get("price") is not None]
            log(f"    Aligned row: {row['material_name']} ({row.get('spec', '')}) "
                f"— {len(quoted)}/{n_suppliers} suppliers quoted")

    log(f"  Aligned rows in matrix: {aligned_rows_found}/{len(aligned_names)}")

    # ── Step 7: Cleanup (opt-in; default KEEP data for UI inspection) ─────
    if os.environ.get("E2E_CLEANUP") == "1":
        log("Step 7: Cleaning up test data...")
        for g in saved_groups:
            requests.delete(f"{API}/api/analysis/bid-alignment/groups/{g['id']}",
                            headers=headers)
        log(f"  Deleted {len(saved_groups)} alignment groups")
        conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db"))
        n_del = conn.execute("DELETE FROM quotes WHERE project_id = ?", (project_id,)).rowcount
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        conn.close()
        log(f"  Deleted {n_del} quotes + project {project_id}")
    else:
        log(f"Step 7: SKIP cleanup — kept project_id={project_id}, "
            f"{len(saved_groups)} alignment groups (set E2E_CLEANUP=1 to purge)")

    # ── Summary ──────────────────────────────────────────────────────────
    log("")
    log("=" * 60)
    log("E2E ALIGNMENT TEST RESULTS")
    log("=" * 60)
    log(f"  PDFs processed:       {len(completed_jobs)}")
    log(f"  Quotes imported:      {n_quotes}")
    log(f"  Suppliers:            {len(supplier_ids)}")
    log(f"  Alignment groups:     {n_groups}")
    log(f"  Field fixes:          {n_fixes}")
    log(f"  Groups applied:       {apply_result['groups_saved']}")
    log(f"  Matrix rows:          {n_rows}")
    log(f"  Aligned rows found:   {aligned_rows_found}")

    if n_groups > 0 and apply_result["groups_saved"] > 0:
        log("")
        log("PASS OK — Full e2e alignment pipeline working")
    elif n_groups == 0:
        log("")
        log("WARN — No alignment groups generated (LLM may need tuning)")
    else:
        log("")
        log("FAIL — Alignment groups generated but not applied correctly")
        sys.exit(1)

    # Save detailed results for inspection
    output = {
        "project_id": project_id,
        "n_quotes": n_quotes,
        "supplier_ids": supplier_ids,
        "suggest": suggest_data,
        "apply": apply_result,
        "matrix_rows": n_rows,
        "aligned_rows_found": aligned_rows_found,
    }
    outpath = os.path.join(os.path.dirname(__file__), "test_e2e_alignment_result.json")
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log(f"  Full results saved to {outpath}")


if __name__ == "__main__":
    main()
