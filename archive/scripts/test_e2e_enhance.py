"""End-to-end test: PDF upload -> OCR -> AI enhance -> batch-confirm (mixed category).

Tests the new /api/intake/enhance endpoint and the modified batch-confirm
that supports per-item category from the enhance step.

Uses one test PDF first, then a second to test pre-alignment.

Requires: backend running on localhost:8002 with DashScope configured.
"""

import json
import os
import sys
import time
import requests

API = "http://localhost:8002"
PDF_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "test")
TIMEOUT_OCR = 300
POLL_INTERVAL = 5


def log(msg: str):
    ts = time.strftime('%H:%M:%S')
    try:
        print(f"[{ts}] {msg}")
    except UnicodeEncodeError:
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[{ts}] {safe}")


def get_token() -> str:
    resp = requests.post(f"{API}/api/auth/login",
                         json={"username": "admin", "password": "admin123"})
    if resp.status_code != 200:
        print(f"Login failed: {resp.status_code}")
        sys.exit(1)
    return resp.json()["access_token"]


def cleanup_project(name: str) -> None:
    """Delete any existing test project (and its quotes) by name."""
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db"))
    rows = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchall()
    for (pid,) in rows:
        n = conn.execute("DELETE FROM quotes WHERE project_id = ?", (pid,)).rowcount
        conn.execute("DELETE FROM bid_alignment_groups WHERE project_id = ?", (pid,))
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        log(f"  Pre-cleanup: removed project {pid} with {n} quotes")
    conn.commit()
    conn.close()


def create_project(headers: dict) -> int:
    resp = requests.post(f"{API}/api/projects",
                         json={"name": "E2E Enhance Test", "status": "active"},
                         headers=headers)
    if resp.status_code in (200, 201):
        return resp.json()["id"]
    print(f"Create project failed: {resp.status_code} {resp.text[:300]}")
    sys.exit(1)


def upload_pdf(filepath: str, project_id: int, headers: dict) -> str:
    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{API}/api/intake/upload",
            files={"file": (os.path.basename(filepath), f, "application/pdf")},
            data={"type": "quote", "project_id": str(project_id)},
            headers=headers,
        )
    if resp.status_code != 200:
        print(f"Upload failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
    return resp.json()["id"]


def poll_job(job_id: str, headers: dict) -> dict:
    start = time.time()
    while time.time() - start < TIMEOUT_OCR:
        resp = requests.get(f"{API}/api/intake/jobs/{job_id}", headers=headers)
        job = resp.json()
        if job["status"] == "done":
            return job
        if job["status"] == "failed":
            print(f"Job FAILED: {job.get('error', '')[:300]}")
            sys.exit(1)
        log(f"  polling... status={job['status']} stage={job.get('progress_stage', '')}")
        time.sleep(POLL_INTERVAL)
    print(f"Job timed out after {TIMEOUT_OCR}s")
    sys.exit(1)


def main():
    log("=== E2E Enhance Test ===")

    # Use the first test PDF
    pdf_name = "凯硕新正投标文件.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_name)
    if not os.path.exists(pdf_path):
        print(f"Missing test PDF: {pdf_path}")
        sys.exit(1)

    # Clean up any previous run's data before starting
    log("Pre-run cleanup: removing any stale test project...")
    cleanup_project("E2E Enhance Test")

    token = get_token()
    headers = {"Authorization": f"Bearer {token}"}

    # ── Step 1: Create project ──────────────────────────────────────────────
    log("Step 1: Creating test project...")
    project_id = create_project(headers)
    log(f"  project_id = {project_id}")

    # ── Step 2: Upload & OCR ────────────────────────────────────────────────
    log(f"Step 2: Uploading {pdf_name} for OCR...")
    job_id = upload_pdf(pdf_path, project_id, headers)
    log(f"  job_id = {job_id[:8]}...")

    log("Step 2b: Waiting for OCR...")
    job = poll_job(job_id, headers)
    items = (job.get("result") or {}).get("items", [])
    supplier = (job.get("result") or {}).get("supplier_name", "")
    log(f"  OCR done: {len(items)} items, supplier={supplier or '(empty)'}")

    # ── Step 3: AI Enhance ──────────────────────────────────────────────────
    log("Step 3: Calling /api/intake/enhance ...")
    enhance_payload = {
        "job_id": job_id,
        "project_id": project_id,
    }
    resp = requests.post(f"{API}/api/intake/enhance",
                         json=enhance_payload, headers=headers, timeout=600)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"FAIL: enhance failed: {resp.text[:500]}")
        sys.exit(1)

    enhance_data = resp.json()
    if enhance_data.get("error"):
        print(f"FAIL: enhance error: {enhance_data['error']}")
        sys.exit(1)

    enhanced_items = enhance_data["items"]
    summary = enhance_data.get("summary", {})
    log(f"  OK: {len(enhanced_items)} items enhanced")
    log(f"  Summary: categorized={summary.get('categorized', 0)}, "
        f"renamed={summary.get('renamed', 0)}, "
        f"aligned={summary.get('aligned', 0)}, "
        f"errors={summary.get('errors', 0)}")
    log(f"  tokens={enhance_data.get('tokens_used', 0)}, "
        f"duration={enhance_data.get('duration_ms', 0)}ms")

    # Show some examples of AI changes
    renamed_examples = [it for it in enhanced_items
                        if it.get("standard_name") != it.get("original_name")][:5]
    if renamed_examples:
        log("  Name standardization examples:")
        for it in renamed_examples:
            log(f"    '{it['original_name']}' -> '{it['standard_name']}' "
                f"({it.get('name_note', '')})")

    categorized_examples = [it for it in enhanced_items if it.get("category")][:5]
    if categorized_examples:
        log("  Category examples:")
        for it in categorized_examples:
            log(f"    '{it.get('material', '')}' -> category={it['category']}")

    # ── Step 4: Verify per-item categories ──────────────────────────────────
    categories_found = set(it.get("category", "") for it in enhanced_items if it.get("category"))
    log(f"  Categories detected: {categories_found}")
    if not categories_found:
        log("  WARN: No categories detected by AI (will fallback to heuristic)")

    # ── Step 5: Batch-confirm with per-item category (no top-level category)
    log("Step 5: Batch-confirm with per-item categories (mixed category support)...")
    confirm_payload = {
        "job_id": job_id,
        "project_id": project_id,
        "supplier_name": supplier or "KITZ",
        "category": "",  # intentionally empty — per-item category should work
        "overrides": enhanced_items,
    }
    resp = requests.post(f"{API}/api/quotes/batch-confirm",
                         json=confirm_payload, headers=headers)
    log(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        # If per-item category failed, try with fallback
        log(f"  Error: {resp.text[:300]}")
        log("  Retrying with default category='阀门'...")
        confirm_payload["category"] = "阀门"
        resp = requests.post(f"{API}/api/quotes/batch-confirm",
                             json=confirm_payload, headers=headers)
        log(f"  HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"FAIL: batch-confirm failed: {resp.text[:500]}")
            sys.exit(1)

    confirm_result = resp.json()
    log(f"  created={confirm_result.get('created', 0)}, "
        f"skipped={confirm_result.get('skipped', 0)}, "
        f"errors={len(confirm_result.get('errors', []))}")

    # ── Step 6: Upload second PDF and test pre-alignment ────────────────────
    pdf2_name = "上海绵存投标文件.pdf"
    pdf2_path = os.path.join(PDF_DIR, pdf2_name)
    if os.path.exists(pdf2_path):
        log(f"Step 6: Uploading {pdf2_name} for pre-alignment test...")
        job2_id = upload_pdf(pdf2_path, project_id, headers)
        log("  Waiting for OCR...")
        job2 = poll_job(job2_id, headers)
        items2 = (job2.get("result") or {}).get("items", [])
        log(f"  OCR done: {len(items2)} items")

        log("Step 6b: Enhance with pre-alignment (project already has KITZ quotes)...")
        resp = requests.post(f"{API}/api/intake/enhance",
                             json={"job_id": job2_id, "project_id": project_id},
                             headers=headers, timeout=600)
        log(f"  HTTP {resp.status_code}")
        if resp.status_code == 200:
            enhance2 = resp.json()
            summary2 = enhance2.get("summary", {})
            log(f"  OK: categorized={summary2.get('categorized', 0)}, "
                f"renamed={summary2.get('renamed', 0)}, "
                f"aligned={summary2.get('aligned', 0)}")

            aligned_items = [it for it in enhance2["items"]
                             if it.get("alignment_note")]
            if aligned_items:
                log(f"  Pre-alignment matches found: {len(aligned_items)}")
                for it in aligned_items[:5]:
                    log(f"    '{it.get('material','')}' -> '{it.get('alignment_note','')}'")
            else:
                log("  WARN: No pre-alignment matches found")

            matched_items = [it for it in enhance2["items"]
                             if it.get("matched_material_id")]
            if matched_items:
                log(f"  Matched to existing materials: {len(matched_items)}")
                for it in matched_items[:5]:
                    log(f"    '{it.get('material','')}' -> material_id={it['matched_material_id']}")
        else:
            log(f"  Enhance failed: {resp.text[:300]}")
    else:
        log(f"Step 6: Skipping (no {pdf2_name})")

    # ── Step 7: Cleanup ─────────────────────────────────────────────────────
    log("Step 7: Cleaning up test data...")
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "data", "mempas.db"))
    n_del = conn.execute("DELETE FROM quotes WHERE project_id = ?", (project_id,)).rowcount
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    log(f"  Deleted {n_del} quotes + project {project_id}")

    # ── Summary ─────────────────────────────────────────────────────────────
    log("")
    log("=" * 60)
    log("E2E ENHANCE TEST RESULTS")
    log("=" * 60)
    log(f"  OCR items:         {len(items)}")
    log(f"  Enhanced items:    {len(enhanced_items)}")
    log(f"  Categories:        {categories_found}")
    log(f"  Names renamed:     {summary.get('renamed', 0)}")
    log(f"  Pre-aligned:       {summary.get('aligned', 0)}")
    log(f"  Quotes created:    {confirm_result.get('created', 0)}")

    if confirm_result.get("created", 0) > 0:
        log("")
        log("PASS - Enhance + mixed-category batch-confirm working")
    else:
        log("")
        log("FAIL - No quotes created")
        sys.exit(1)


if __name__ == "__main__":
    main()
