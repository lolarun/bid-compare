"""Extract per-page OCR HTML from the extraction_jobs.result JSON for sub18.

Outputs pages 9-16 (PDF pages) to fresh_db_p{N}.html files.

Run from repo root:
  python scripts/extract_ocr_html.py
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "outputs" / "submission_reextract_audit"

conn = sqlite3.connect(str(ROOT / "data" / "mempas.db"))
cur = conn.cursor()
cur.execute(
    "SELECT result FROM extraction_jobs WHERE id = ?",
    ("490dcd878d7e4113b60ae9defab93f82",)
)
row = cur.fetchone()
conn.close()

if not row or not row[0]:
    print("No result found for sub18 extraction job")
    exit(1)

result = json.loads(row[0])
pages = result.get("pages", [])
print(f"Total pages in extraction result: {len(pages)}")

# Dump pages 9-16 (1-indexed) → these are pages with items 50-89
for i, page in enumerate(pages):
    page_num = i + 1
    if 9 <= page_num <= 16:
        html = page.get("html", "")
        if not html:
            raw = page.get("raw", "")
            html = raw if raw else ""

        # Try common keys
        if not html:
            for key in ("ocr_html", "content", "text"):
                val = page.get(key, "")
                if val:
                    html = val
                    break

        out_file = OUT_DIR / f"fresh_db_p{page_num:02d}.html"
        if html:
            out_file.write_text(html, encoding="utf-8")
            print(f"  Page {page_num}: {len(html)} chars → {out_file.name}")
        else:
            print(f"  Page {page_num}: no HTML content. Keys: {list(page.keys())}")

# Also print the top-level keys of the result
print(f"\nTop-level result keys: {list(result.keys())}")
if pages:
    print(f"First page keys: {list(pages[0].keys())}")
