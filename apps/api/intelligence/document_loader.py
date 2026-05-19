"""Document loader — convert any uploaded file into a list of image bytes.

Supported inputs:
- PDF (.pdf): rendered via pypdfium2; each page → PNG bytes
- Images (.png/.jpg/.jpeg): pass through (optional resize)
- Excel (.xlsx/.xls): NOT handled here; Excel uses import_service directly

We render PDF pages at 2x scale for OCR quality, capped at MAX_PAGES.
"""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

MAX_PAGES = 12          # cap multi-page PDFs to keep LLM token usage sane
RENDER_SCALE = 2.0      # 2× DPI for clearer table OCR
MAX_EDGE_PX = 2400      # downscale large images so prompts stay within token limits


class DocumentLoader:
    """Stateless utility — load any file into a list[bytes] of PNGs."""

    @staticmethod
    def to_images(file_path: str | Path) -> list[bytes]:
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return DocumentLoader._pdf_to_images(path)
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return [DocumentLoader._normalize_image(path.read_bytes())]
        raise ValueError(
            f"Unsupported file extension for vision extraction: {suffix}. "
            "Use PDF or image files; Excel goes through import_service."
        )

    @staticmethod
    def _pdf_to_images(path: Path) -> list[bytes]:
        pdf = pdfium.PdfDocument(str(path))
        try:
            pages = min(len(pdf), MAX_PAGES)
            images: list[bytes] = []
            for i in range(pages):
                page = pdf[i]
                pil_image = page.render(scale=RENDER_SCALE).to_pil()
                images.append(DocumentLoader._pil_to_png_bytes(pil_image))
            return images
        finally:
            pdf.close()

    @staticmethod
    def _normalize_image(data: bytes) -> bytes:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > MAX_EDGE_PX:
                scale = MAX_EDGE_PX / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            return DocumentLoader._pil_to_png_bytes(img)

    @staticmethod
    def _pil_to_png_bytes(img: Image.Image) -> bytes:
        # Downscale very large pages too
        w, h = img.size
        longest = max(w, h)
        if longest > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
