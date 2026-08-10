"""scripts/scan_taikelong_pages.py

Scan ALL pages of the 泰科龙 PDF (sub18).
- Reuses existing tmp_p5.html … tmp_p12.html for pages 5-12 (no extra OCR cost)
- OCRs pages 1-4 and 13-N fresh
- Detects transposed table signatures per page
- Outputs: outputs/submission_reextract_audit/taikelong_page_inventory.csv

Usage:
    python scripts/scan_taikelong_pages.py [--max-pages N]
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("apps/api/.env")

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
from apps.api.intelligence.table_parser import html_to_table_grids

PDF_PATH = "data/uploads/20260618/e47785b859684edaa2fa7d96e518ff302433fe739673afa6cf8b963e214621a7.pdf"

# Pre-computed HTML for pages 5-12 (reuse to avoid re-OCR cost)
CACHED_HTML: dict[int, str] = {
    5:  "tmp_p5.html",
    6:  "tmp_p6.html",
    7:  "tmp_p7.html",
    8:  "tmp_p8.html",
    9:  "tmp_p9.html",
    10: "tmp_p10.html",
    11: "tmp_p11.html",
    12: "tmp_p12.html",
}

_FLOAT_RE = re.compile(r"[\d,，]+\.?\d*")
_SEQ_INT_RE = re.compile(r"^\s*\d+\s*$")

OUTPUT_DIR = Path("outputs/submission_reextract_audit")


def read_cached(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    # strip markdown fence if present
    if content.startswith("```"):
        lines = [l for l in content.splitlines() if l not in ("```html", "```", "```json")]
        content = "\n".join(lines).strip()
    return content


def detect_transposed(html: str) -> tuple[bool, str]:
    """Heuristic: last row of any table has 3+ sequential integers -> transposed."""
    grids = html_to_table_grids(html, page_num=0)
    if grids:
        return False, "standard_grid"

    # Parse raw HTML for tables; check last row for sequential ints
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables: list[list[list[str]]] = []
            self._cur_table: list[list[str]] = []
            self._cur_row: list[str] = []
            self._cur_cell = ""
            self._in_cell = False

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self._cur_table = []
            elif tag in ("tr",):
                self._cur_row = []
            elif tag in ("td", "th"):
                self._cur_cell = ""
                self._in_cell = True

        def handle_endtag(self, tag):
            if tag == "table" and self._cur_table:
                self.tables.append(self._cur_table)
                self._cur_table = []
            elif tag == "tr" and self._cur_row is not None:
                self._cur_table.append(self._cur_row)
                self._cur_row = []
            elif tag in ("td", "th"):
                self._cur_row.append(self._cur_cell.strip())
                self._in_cell = False

        def handle_data(self, data):
            if self._in_cell:
                self._cur_cell += data

    parser = _TableParser()
    parser.feed(html)

    for table in parser.tables:
        if len(table) < 4:
            continue
        last_row = [c for c in table[-1] if c.strip()]
        if len(last_row) < 3:
            continue
        # Check if last row cells are sequential integers
        nums = []
        for c in last_row:
            m = re.match(r"^\s*(\d+)\s*$", c)
            if m:
                nums.append(int(m.group(1)))
        if len(nums) >= 3 and len(nums) == len(last_row):
            # Check sequential: sorted and consecutive
            s = sorted(nums)
            if all(s[i+1] - s[i] == 1 for i in range(len(s)-1)):
                return True, f"last_row_sequential_ints={last_row[:5]}"
    return False, "no_sequential_last_row"


def extract_numbers_from_html(html: str) -> list[float]:
    """Extract all numbers >= 1 from html that look like prices."""
    nums = []
    for m in _FLOAT_RE.finditer(html):
        try:
            v = float(m.group().replace(",", "").replace("，", ""))
            if v >= 1.0:
                nums.append(v)
        except ValueError:
            pass
    return nums


def find_subtotals(html: str) -> list[float]:
    """Find values near keywords like 合计/小计/汇总."""
    from html.parser import HTMLParser

    class _Row(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self._cur: list[str] = []
            self._cell = ""
            self._in = False

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._cur = []
            elif tag in ("td", "th"):
                self._cell = ""
                self._in = True

        def handle_endtag(self, tag):
            if tag == "tr":
                self.rows.append(self._cur[:])
                self._cur = []
            elif tag in ("td", "th"):
                self._cur.append(self._cell.strip())
                self._in = False

        def handle_data(self, data):
            if self._in:
                self._cell += data

    p = _Row()
    p.feed(html)
    result = []
    keywords = re.compile(r"合计|小计|汇总|总计|合价|含税")
    for row in p.rows:
        row_text = " ".join(row)
        if keywords.search(row_text):
            for cell in row:
                try:
                    v = float(cell.replace(",", "").replace("，", "").strip())
                    if v > 100:
                        result.append(v)
                except ValueError:
                    pass
    return result


def scan_page(page_num: int, html: str) -> dict:
    grids = html_to_table_grids(html, page_num=page_num)
    is_transposed, trans_reason = detect_transposed(html)
    subtotals = find_subtotals(html)

    # Count visible rows in grids
    grid_rows = sum(len(g.rows) for g in grids)
    # Classify role
    from apps.api.intelligence.page_classifier import classify_page
    cls = classify_page(html)
    role = cls.primary_role.value if hasattr(cls.primary_role, "value") else str(cls.primary_role)

    # Count data rows in raw HTML tables (any TD rows not header)
    raw_row_count = len(re.findall(r"<tr", html, re.IGNORECASE))

    # Extract largest numbers as potential totals
    top_nums = sorted(find_subtotals(html), reverse=True)[:5]

    return {
        "page": page_num,
        "role": role,
        "grids": len(grids),
        "grid_rows": grid_rows,
        "raw_tr_count": raw_row_count,
        "transposed": is_transposed,
        "trans_reason": trans_reason,
        "subtotals": ";".join(f"{v:,.2f}" for v in sorted(top_nums, reverse=True)),
        "html_len": len(html),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    from apps.api.intelligence.document_loader import MAX_PAGES_UNLIMITED
    log.info("Loading PDF: %s", PDF_PATH)
    limit = args.max_pages if args.max_pages else MAX_PAGES_UNLIMITED
    images = DocumentLoader.to_images(PDF_PATH, max_pages=limit)
    total = len(images)
    log.info("Total pages: %d", total)

    provider = DashScopeOCRProvider()
    results = []

    # Collect HTML for all pages (reuse cached, OCR the rest concurrently)
    html_by_page: dict[int, str] = {}

    for page_num in range(1, total + 1):
        cache_path = CACHED_HTML.get(page_num)
        if cache_path and os.path.exists(cache_path):
            html_by_page[page_num] = read_cached(cache_path)

    pages_to_ocr = [(i+1, images[i]) for i in range(total) if (i+1) not in html_by_page]
    log.info("Cached: %d pages | Need OCR: %d pages", len(html_by_page), len(pages_to_ocr))

    if pages_to_ocr:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _ocr_one(page_num: int, img: bytes) -> tuple[int, str]:
            html, _ = provider._ocr_page(img)
            return page_num, html

        workers = min(24, len(pages_to_ocr))
        with ThreadPoolExecutor(max_workers=workers) as exc:
            futs = {exc.submit(_ocr_one, pn, img): pn for pn, img in pages_to_ocr}
            done = 0
            for fut in as_completed(futs):
                pn, html = fut.result()
                html_by_page[pn] = html
                # Save to tmp file for inspection
                out_path = f"tmp_p{pn}_fresh.html"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(html)
                done += 1
                if done % 5 == 0 or done == len(pages_to_ocr):
                    log.info("OCR progress: %d/%d pages done", done, len(pages_to_ocr))

    for i in range(total):
        page_num = i + 1
        html = html_by_page.get(page_num, "")
        info = scan_page(page_num, html)
        results.append(info)
        log.info(
            "Page %2d: role=%-14s grids=%d grid_rows=%d transposed=%-5s subtotals=%s",
            page_num, info["role"], info["grids"], info["grid_rows"],
            info["transposed"], info["subtotals"] or "—",
        )

    # Write CSV
    csv_path = OUTPUT_DIR / "taikelong_page_inventory.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    log.info("Saved inventory to %s", csv_path)

    # Summary
    quote_pages = [r for r in results if "quote" in r["role"]]
    transposed_pages = [r for r in results if r["transposed"]]
    total_subtotals: list[float] = []
    for r in results:
        for s in r["subtotals"].split(";"):
            s = s.strip()
            if s:
                try:
                    total_subtotals.append(float(s.replace(",", "")))
                except ValueError:
                    pass

    print()
    print("=" * 60)
    print(f"  Total pages:        {total}")
    print(f"  Quote pages:        {len(quote_pages)}")
    print(f"  Transposed pages:   {len(transposed_pages)}")
    print(f"  Pages with grids:   {sum(1 for r in results if r['grids'] > 0)}")
    print(f"  Quote page list:    {[r['page'] for r in quote_pages]}")
    print(f"  Transposed list:    {[r['page'] for r in transposed_pages]}")
    print(f"  Known total:        1,067,616.41")
    print("=" * 60)


if __name__ == "__main__":
    main()
