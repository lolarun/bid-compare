"""Extract text directly from PDF pages using pypdfium2 (no OCR needed).

For PDFs with embedded text, this is far more reliable than OCR.
Outputs text for pages 7-12 of sub18 PDF.

Run from repo root:
  python scripts/extract_pdf_text.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent.parent
SUB18_PDF = (ROOT / "data" / "uploads" / "20260618"
             / "e47785b859684edaa2fa7d96e518ff302433fe739673afa6cf8b963e214621a7.pdf")
OUT_DIR = ROOT / "outputs" / "submission_reextract_audit"


def extract_page_text(pdf_path: Path, page_idx: int) -> str:
    """Extract all text from a PDF page using embedded text layer."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_idx]
        textpage = page.get_textpage()
        return textpage.get_text_range()
    finally:
        doc.close()


def extract_page_text_with_positions(pdf_path: Path, page_idx: int) -> list[tuple]:
    """Extract text with positions for each character."""
    doc = pdfium.PdfDocument(str(pdf_path))
    results = []
    try:
        page = doc[page_idx]
        textpage = page.get_textpage()
        count = textpage.count_chars()
        print(f"  Page {page_idx+1}: {count} characters")
        # Get text as plain string
        text = textpage.get_text_range()
        results = list(text)
    finally:
        doc.close()
    return results


def main():
    if not SUB18_PDF.exists():
        print(f"ERROR: PDF not found: {SUB18_PDF}")
        sys.exit(1)

    doc = pdfium.PdfDocument(str(SUB18_PDF))
    total_pages = len(doc)
    doc.close()
    print(f"PDF has {total_pages} pages total")

    # Extract pages 7-14 (1-indexed) → indices 6-13
    for page_num in range(7, 15):
        page_idx = page_num - 1
        print(f"\n=== Page {page_num} (idx={page_idx}) ===")
        text = extract_page_text(SUB18_PDF, page_idx)
        out_file = OUT_DIR / f"pdf_text_p{page_num:02d}.txt"
        out_file.write_text(text, encoding="utf-8")
        # Show first 500 chars
        preview = text[:500].replace('\n', '↵')
        print(f"  {len(text)} chars extracted")
        print(f"  Preview: {preview!r}")


if __name__ == "__main__":
    main()
