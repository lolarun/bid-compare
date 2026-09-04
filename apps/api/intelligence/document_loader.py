"""Document loader — convert any uploaded file into a list of image bytes.

Supported inputs:
- PDF (.pdf): rendered via pypdfium2; each page → PNG bytes
- Images (.png/.jpg/.jpeg): pass through (optional resize)
- Excel (.xlsx/.xls): NOT handled here; Excel uses import_service directly

We render PDF pages at 2x scale for OCR quality, capped at MAX_PAGES.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from apps.api.core.config import get_settings

MAX_PAGES = 12          # default cap for to_images() (legacy per-page path, no production caller
                        # since 2026-08-11 — VL-direct uses get_page_count/render_pages instead)
MAX_PAGES_UNLIMITED = 200  # high ceiling for role-aware path that classifies all pages
# Layer 0: render quality is env-driven (OCR_RENDER_SCALE / OCR_MAX_EDGE_PX).
RENDER_SCALE = get_settings().OCR_RENDER_SCALE   # PDF render DPI multiplier
MAX_EDGE_PX = get_settings().OCR_MAX_EDGE_PX      # downscale cap to stay within token limits

# PDFium 不是线程安全的：跨线程并发调用会在原生层触发非法指令
# （Windows 0xc000001d），且不是必现，而是概率性的——七份并发跑过一次 5/7 崩、
# 一次 7/7 崩。因此**所有** pdfium 入口（含只读的 get_page_count）必须整体串行。
# 可重入是必要的：同一线程里 render_pages → _render_page_pdfium 是嵌套持有。
#
# 这里刻意不保留"并发数可配"——把它做成信号量就是在赌能不能撞上，
# 而崩溃发生在原生层，Python 侧只看得到一个 OSError，无从定位。
_PDF_LOCK = threading.RLock()


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
    def to_thumbnails(
        file_path: str | Path,
        max_pages: int | None = None,
        thumb_edge_px: int = 1024,
    ) -> list[bytes]:
        """Render all PDF pages as LOW-RES thumbnails for visual page classification."""
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            cap = max_pages or MAX_PAGES_UNLIMITED
            with _PDF_LOCK:
                doc = pdfium.PdfDocument(str(path))
                try:
                    pages = min(len(doc), cap)
                    return [DocumentLoader._render_thumb_pdfium(doc, i, thumb_edge_px) for i in range(pages)]
                finally:
                    doc.close()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            with Image.open(io.BytesIO(path.read_bytes())) as im:
                return [DocumentLoader._downscale_png(im.convert("RGB"), thumb_edge_px)]
        raise ValueError(f"Unsupported file for thumbnails: {suffix}")

    @staticmethod
    def get_page_count(file_path: str | Path) -> int:
        """Return actual PDF page count without rendering any pages."""
        path = Path(file_path)
        if path.suffix.lower() != ".pdf":
            return 1
        with _PDF_LOCK:
            doc = pdfium.PdfDocument(str(path))
            try:
                return len(doc)
            finally:
                doc.close()

    @staticmethod
    def _downscale_png(img: Image.Image, edge_px: int) -> bytes:
        w, h = img.size
        longest = max(w, h)
        if longest > edge_px:
            scale = edge_px / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=3)
        return buf.getvalue()

    @staticmethod
    def _pdf_to_images(path: Path, max_pages: int = MAX_PAGES) -> list[bytes]:
        with _PDF_LOCK:
            doc = pdfium.PdfDocument(str(path))
            try:
                pages = min(len(doc), max_pages)
                return [DocumentLoader._render_page_pdfium(doc, i) for i in range(pages)]
            finally:
                doc.close()

    @staticmethod
    def _render_page_pdfium(doc, index0: int) -> bytes:
        """Render one page with pypdfium2 and normalize it for OCR."""
        page = doc[index0]
        try:
            bitmap = page.render(scale=RENDER_SCALE)
            return DocumentLoader._pil_to_png_bytes(bitmap.to_pil().convert("RGB"))
        finally:
            page.close()

    @staticmethod
    def _render_thumb_pdfium(doc, index0: int, thumb_edge_px: int) -> bytes:
        """Render one page to a downscaled thumbnail with pypdfium2."""
        page = doc[index0]
        try:
            bitmap = page.render(scale=1)
            return DocumentLoader._downscale_png(bitmap.to_pil().convert("RGB"), thumb_edge_px)
        finally:
            page.close()

    @staticmethod
    def render_pages(
        file_path: str | Path, page_numbers: list[int]
    ) -> dict[int, bytes]:
        """Render ONLY the given 1-based pages to full-res PNG bytes."""
        path = Path(file_path)
        suffix = path.suffix.lower()
        wanted = sorted({p for p in page_numbers if p >= 1})
        if not wanted:
            return {}
        if suffix == ".pdf":
            out: dict[int, bytes] = {}
            with _PDF_LOCK:
                doc = pdfium.PdfDocument(str(path))
                try:
                    n = len(doc)
                    for p in wanted:
                        if p <= n:
                            out[p] = DocumentLoader._render_page_pdfium(doc, p - 1)
                    return out
                finally:
                    doc.close()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            return {1: DocumentLoader._normalize_image(path.read_bytes())} if 1 in wanted else {}
        raise ValueError(f"Unsupported file for render_pages: {suffix}")

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
