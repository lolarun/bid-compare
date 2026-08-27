"""Crop pages 11 and 12 of sub18 PDF into Q1/Q2 strips and OCR each strip.

Run from repo root:
  python scripts/crop_p11_p12.py

Outputs: outputs/submission_reextract_audit/fresh_p11_Q*.html, fresh_p12_Q*.html
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

# ── env / path setup ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ENV_PATH = ROOT / "apps" / "api" / ".env"

# Load .env manually (no python-dotenv dependency required)
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import pypdfium2 as pdfium
from PIL import Image
import openai

API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
OCR_MODEL = os.environ.get("DASHSCOPE_OCR_MODEL", "qwen-vl-ocr-latest")

SUB18_PDF = ROOT / "data" / "uploads" / "20260618" / "e47785b859684edaa2fa7d96e518ff302433fe739673afa6cf8b963e214621a7.pdf"
OUT_DIR = ROOT / "outputs" / "submission_reextract_audit"


def render_page(pdf_path: Path, page_idx: int, scale: float = 2.0) -> Image.Image:
    """Render a single PDF page to a PIL Image."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_idx]
        pil_img = page.render(scale=scale).to_pil()
        return pil_img.convert("RGB")
    finally:
        pdf.close()


def crop_strip(img: Image.Image, x_start_pct: float, x_end_pct: float) -> Image.Image:
    """Crop a horizontal percentage strip from an image."""
    w, h = img.size
    x0 = int(w * x_start_pct)
    x1 = int(w * x_end_pct)
    return img.crop((x0, 0, x1, h))


def pil_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ocr_image_bytes(image_bytes: bytes) -> str:
    """Call DashScope qwen-vl-ocr to get HTML table from image bytes."""
    client = openai.OpenAI(api_key=API_KEY, base_url=BASE_URL)
    b64 = base64.b64encode(image_bytes).decode()
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


def format_html(raw: str) -> str:
    """Return the raw OCR output; if it's not HTML, wrap in <pre>."""
    stripped = raw.strip()
    if stripped.startswith("<"):
        return stripped
    return f"<pre>{stripped}</pre>"


def crop_h_strip(img: Image.Image, y_start_pct: float, y_end_pct: float) -> Image.Image:
    """Crop a vertical percentage strip from an image (by height)."""
    w, h = img.size
    y0 = int(h * y_start_pct)
    y1 = int(h * y_end_pct)
    return img.crop((0, y0, w, y1))


def process_page(page_idx: int, label: str, landscape: bool = False, force: bool = False):
    """Render page, crop Q1/Q2, OCR each, save HTML files."""
    print(f"\n=== Page {page_idx + 1} ({label}) ===")
    img = render_page(SUB18_PDF, page_idx)
    w, h = img.size
    is_landscape = w > h
    print(f"  Full size: {w}x{h} ({'landscape' if is_landscape else 'portrait'})")

    if is_landscape:
        # For landscape pages with standard table layout:
        # items run top-to-bottom, attributes run left-to-right
        # → crop by HEIGHT into horizontal bands
        strips = [
            ("TOP", None, 0.00, 0.50),   # top half (header + first items)
            ("BTM", None, 0.50, 1.00),   # bottom half (remaining items)
            ("FULL", None, 0.00, 1.00),  # full page
        ]
    else:
        # Portrait transposed table: items in columns, crop by WIDTH
        strips = [
            ("Q1", (0.00, 0.25), None, None),
            ("Q2", (0.25, 0.50), None, None),
            ("Q3", (0.50, 0.75), None, None),
        ]

    for strip_name, w_range, h_start, h_end in strips:
        out_file = OUT_DIR / f"fresh_{label}_{strip_name}.html"
        if out_file.exists() and not force:
            print(f"  {strip_name}: already exists, skipping")
            continue

        if w_range is not None:
            cropped = crop_strip(img, w_range[0], w_range[1])
        else:
            cropped = crop_h_strip(img, h_start, h_end)

        print(f"  {strip_name}: crop size {cropped.size}, OCR-ing...")
        png_bytes = pil_to_png_bytes(cropped)
        try:
            raw = ocr_image_bytes(png_bytes)
            html = format_html(raw)
            out_file.write_text(html, encoding="utf-8")
            row_count = html.count("<tr>")
            cell_count = html.count("<td>")
            print(f"  {strip_name}: {row_count} rows, {cell_count} cells → {out_file.name}")
        except Exception as e:
            print(f"  {strip_name}: ERROR — {e}")
            out_file.write_text(f"<pre>ERROR: {e}</pre>", encoding="utf-8")


if __name__ == "__main__":
    if not API_KEY:
        print("ERROR: DASHSCOPE_API_KEY not set")
        sys.exit(1)
    if not SUB18_PDF.exists():
        print(f"ERROR: PDF not found at {SUB18_PDF}")
        sys.exit(1)

    # Page 11 = index 10; Page 12 = index 11
    process_page(10, "p11", force=True)
    process_page(11, "p12", force=True)
    print("\nDone.")
