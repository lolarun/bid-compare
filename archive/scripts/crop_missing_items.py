"""Crop pages 7, 9, and 10 of sub18 PDF to recover missing items.

Targets:
  page 7  narrow strips → items 27-30 (橡胶瓣止回阀 DN50-DN100)
  page 9  Q1/Q2         → items 50-58 (completely missing from DB)
  page 9  FULL          → reference view of page 9 layout
  page 10 strip 22-35%  → items 63-64 boundary region

Run from repo root:
  python scripts/crop_missing_items.py
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

# ── env / path setup ────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ENV_PATH = ROOT / "apps" / "api" / ".env"

if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import pypdfium2 as pdfium
from PIL import Image
import openai

API_KEY  = os.environ.get("DASHSCOPE_API_KEY", "")
BASE_URL = os.environ.get("DASHSCOPE_BASE_URL",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1")
OCR_MODEL = os.environ.get("DASHSCOPE_OCR_MODEL", "qwen-vl-ocr-latest")

SUB18_PDF = (ROOT / "data" / "uploads" / "20260618"
             / "e47785b859684edaa2fa7d96e518ff302433fe739673afa6cf8b963e214621a7.pdf")
OUT_DIR = ROOT / "outputs" / "submission_reextract_audit"


def render_page(pdf_path: Path, page_idx: int, scale: float = 2.5) -> Image.Image:
    """Render a single PDF page to a PIL Image at the given scale."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_idx]
        return page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()


def crop_w(img: Image.Image, x0_pct: float, x1_pct: float) -> Image.Image:
    """Vertical strip crop by percentage of width."""
    w, h = img.size
    return img.crop((int(w * x0_pct), 0, int(w * x1_pct), h))


def pil_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ocr(png_bytes: bytes) -> str:
    client = openai.OpenAI(api_key=API_KEY, base_url=BASE_URL)
    b64 = base64.b64encode(png_bytes).decode()
    response = client.chat.completions.create(
        model=OCR_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                    "min_pixels": 28 * 28 * 4,
                    "max_pixels": 1280 * 28 * 28,
                },
                {"type": "text", "text": "Read the text in the image."},
            ],
        }],
    )
    return response.choices[0].message.content or ""


def do_crop(page_idx: int, label: str, x0: float, x1: float, force: bool = False):
    """Render page, crop vertical strip [x0, x1], OCR, save HTML."""
    out_file = OUT_DIR / f"fresh_{label}.html"
    if out_file.exists() and not force:
        print(f"  {label}: already exists, skipping")
        return

    print(f"  {label}: rendering page {page_idx + 1}...")
    img = render_page(SUB18_PDF, page_idx)
    w, h = img.size
    print(f"  {label}: page size {w}×{h}, crop [{x0:.0%}–{x1:.0%}]")

    cropped = crop_w(img, x0, x1)
    png_bytes = pil_to_png_bytes(cropped)

    print(f"  {label}: OCR-ing ({len(png_bytes)//1024} KB)...")
    try:
        raw = ocr(png_bytes)
        html = raw.strip() if raw.strip().startswith("<") else f"<pre>{raw}</pre>"
        out_file.write_text(html, encoding="utf-8")
        rows = html.count("<tr")
        cells = html.count("<td")
        print(f"  {label}: {rows} rows, {cells} cells → {out_file.name}")
    except Exception as e:
        print(f"  {label}: ERROR — {e}")
        out_file.write_text(f"<pre>ERROR: {e}</pre>", encoding="utf-8")


if __name__ == "__main__":
    if not API_KEY:
        print("ERROR: DASHSCOPE_API_KEY not set"); sys.exit(1)
    if not SUB18_PDF.exists():
        print(f"ERROR: PDF not found at {SUB18_PDF}"); sys.exit(1)

    print("===== Page 7 (idx=6): narrow strips for items 27-30 =====")
    # Items 24-30 fill Q1 (0-25%); items 27-30 are the rightmost 4 of those 7 columns
    # Each column ≈ 25%/7 = 3.6% wide; item 27 starts at ~10.7%, item 30 ends at ~25%
    do_crop(6, "p7_27to30",  0.10, 0.25, force=True)   # items 27-30 area
    do_crop(6, "p7_28to30",  0.14, 0.25, force=True)   # items 28-30 (narrower)
    do_crop(6, "p7_29to30",  0.18, 0.25, force=True)   # items 29-30 (very narrow)

    print("\n===== Page 9 (idx=8): Q1/Q2/Q3/FULL — unknown items =====")
    do_crop(8, "p9_Q1",   0.00, 0.25, force=True)
    do_crop(8, "p9_Q2",   0.25, 0.50, force=True)
    do_crop(8, "p9_Q3",   0.50, 0.75, force=True)
    do_crop(8, "p9_FULL", 0.00, 1.00, force=True)

    print("\n===== Page 10 (idx=9): strip 22-40% for items 63-64 boundary =====")
    do_crop(9, "p10_22to40", 0.22, 0.40, force=True)

    print("\nDone.")
