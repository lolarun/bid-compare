"""Document loader — convert any uploaded file into a list of image bytes.

Supported inputs:
- PDF (.pdf): rendered via pypdfium2; each page → PNG bytes
- Images (.png/.jpg/.jpeg): pass through (optional resize)
- Excel (.xlsx/.xls): NOT handled here; Excel uses import_service directly

We render PDF pages at 2x scale for OCR quality, capped at MAX_PAGES.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from apps.api.core.config import get_settings

MAX_PAGES = 12          # default cap for _run_batched (old path); role-aware path ignores this
MAX_PAGES_UNLIMITED = 200  # high ceiling for role-aware path that classifies all pages
# Layer 0: render quality is env-driven (OCR_RENDER_SCALE / OCR_MAX_EDGE_PX).
RENDER_SCALE = get_settings().OCR_RENDER_SCALE   # PDF render DPI multiplier
MAX_EDGE_PX = get_settings().OCR_MAX_EDGE_PX      # downscale cap to stay within token limits

# Limit concurrent PDF renders to avoid saturating CPU when N files arrive together.
# Each render is CPU-bound (pypdfium2 page rasterisation); 2 is safe on a 4-core host.
_PDF_RENDER_SEM = threading.Semaphore(
    max(1, int(os.getenv("PDF_RENDER_CONCURRENCY", "2")))
)


class DocumentLoader:
    """Stateless utility — load any file into a list[bytes] of PNGs."""

    @staticmethod
    def to_images(file_path: str | Path, max_pages: int | None = None) -> list[bytes]:
        """Convert file to a list of PNG images.

        Args:
            max_pages: cap on number of pages. None = use MAX_PAGES default (12).
                       Pass MAX_PAGES_UNLIMITED for role-aware pipelines that
                       need all pages for classification.
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return DocumentLoader._pdf_to_images(path, max_pages=max_pages or MAX_PAGES)
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return [DocumentLoader._normalize_image(path.read_bytes())]
        raise ValueError(
            f"Unsupported file extension for vision extraction: {suffix}. "
            "Use PDF or image files; Excel goes through import_service."
        )

    @staticmethod
    def _pdf_to_images(path: Path, max_pages: int = MAX_PAGES) -> list[bytes]:
        with _PDF_RENDER_SEM:
            pdf = pdfium.PdfDocument(str(path))
            try:
                pages = min(len(pdf), max_pages)
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
        img.save(buf, format="PNG", compress_level=1)
        return buf.getvalue()
