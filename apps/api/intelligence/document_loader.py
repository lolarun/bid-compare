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
from concurrent.futures import ThreadPoolExecutor
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
    max(1, int(os.getenv("PDF_RENDER_CONCURRENCY", "3")))
)
# Pages rendered in parallel within a single document.
# pypdfium2 releases the GIL during C-level rendering, so threads achieve true parallelism
# as long as each thread opens its own PdfDocument (no shared object).
_RENDER_WORKERS = max(1, int(os.getenv("PDF_RENDER_WORKERS", "4")))


def _render_one_thumb(file_path: str, index0: int, thumb_edge_px: int) -> bytes:
    """Worker: open own PdfDocument, render one thumbnail, close, return PNG bytes."""
    pdf = pdfium.PdfDocument(file_path)
    try:
        return DocumentLoader._render_thumb_png(pdf, index0, thumb_edge_px)
    finally:
        pdf.close()


def _render_one_page(file_path: str, index0: int) -> bytes:
    """Worker: open own PdfDocument, render one full-res page, close, return PNG bytes."""
    pdf = pdfium.PdfDocument(file_path)
    try:
        return DocumentLoader._render_page_png(pdf, index0)
    finally:
        pdf.close()


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
        """Render all PDF pages as LOW-RES thumbnails for visual page classification.

        Distinct from to_images (full-res for OCR). Thumbnails keep the visual
        layout (table grid vs prose vs cert) recognisable while staying small/cheap
        for batched multi-image VL classification.

        Args:
            thumb_edge_px: longest-edge cap in pixels (default 1024).
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            cap = max_pages or MAX_PAGES_UNLIMITED
            with _PDF_RENDER_SEM:
                # 先取页数（不渲染），再按需并行渲染
                pdf = pdfium.PdfDocument(str(path))
                pages = min(len(pdf), cap)
                pdf.close()

                workers = min(_RENDER_WORKERS, pages)
                with ThreadPoolExecutor(max_workers=workers) as exc:
                    results = list(exc.map(
                        lambda i: _render_one_thumb(str(path), i, thumb_edge_px),
                        range(pages),
                    ))
                return results
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
        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()

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
        with _PDF_RENDER_SEM:
            pdf = pdfium.PdfDocument(str(path))
            try:
                pages = min(len(pdf), max_pages)
                workers = min(_RENDER_WORKERS, pages)
                with ThreadPoolExecutor(max_workers=workers) as exc:
                    results = list(exc.map(
                        lambda i: _render_one_page(str(path), i),
                        range(pages),
                    ))
                return results
            finally:
                pdf.close()

    @staticmethod
    def _close_quietly(*objs) -> None:
        """Best-effort close of pypdfium2 page/bitmap (and PIL) to free native memory now."""
        for o in objs:
            try:
                if o is not None and hasattr(o, "close"):
                    o.close()
            except Exception:
                pass

    @staticmethod
    def _render_page_png(pdf: "pdfium.PdfDocument", index0: int) -> bytes:
        """Render one page (0-based index) to full-res PNG bytes.

        Single source of truth for the full-res render+encode path, shared by
        _pdf_to_images and render_pages so their output is byte-identical
        (critical: OCR snapshots are keyed by SHA256(image_bytes)).
        Explicitly closes page/bitmap/PIL after encoding so per-page native
        memory is freed immediately (scanned PDFs accumulate ~30MB/page otherwise).
        """
        page = pdf[index0]
        bitmap = page.render(scale=RENDER_SCALE)
        pil_image = bitmap.to_pil()
        try:
            return DocumentLoader._pil_to_png_bytes(pil_image)
        finally:
            DocumentLoader._close_quietly(pil_image, bitmap, page)

    @staticmethod
    def _render_thumb_png(pdf: "pdfium.PdfDocument", index0: int, thumb_edge_px: int) -> bytes:
        """Render one page to a downscaled thumbnail PNG, releasing native memory per page."""
        page = pdf[index0]
        bitmap = page.render(scale=1.0)
        pil = bitmap.to_pil().convert("RGB")
        try:
            return DocumentLoader._downscale_png(pil, thumb_edge_px)
        finally:
            DocumentLoader._close_quietly(pil, bitmap, page)

    @staticmethod
    def render_pages(
        file_path: str | Path, page_numbers: list[int]
    ) -> dict[int, bytes]:
        """Render ONLY the given 1-based pages to full-res PNG bytes.

        Returns {page_no: png_bytes}. Bytes are identical to to_images()[page-1]
        for the same page (same RENDER_SCALE, same encode), so OCR snapshot
        hashes are unchanged. Guarded by the same _PDF_RENDER_SEM as to_images.
        Out-of-range pages are skipped. Non-PDF single images map to page 1.
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        wanted = sorted({p for p in page_numbers if p >= 1})
        if not wanted:
            return {}
        if suffix == ".pdf":
            with _PDF_RENDER_SEM:
                pdf = pdfium.PdfDocument(str(path))
                n = len(pdf)
                pdf.close()
                valid = [p for p in wanted if p <= n]
                if not valid:
                    return {}
                workers = min(_RENDER_WORKERS, len(valid))
                with ThreadPoolExecutor(max_workers=workers) as exc:
                    results = list(exc.map(
                        lambda p: _render_one_page(str(path), p - 1),
                        valid,
                    ))
                return dict(zip(valid, results))
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
