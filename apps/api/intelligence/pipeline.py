"""ExtractionPipeline — orchestrates loader → splitter → provider → aggregator → post-processing.

Two public methods:
- extract_tender(file_path) → ExtractionResponse (data matches TENDER_SCHEMA)
- extract_quote(file_path, context) → ExtractionResponse (data matches QUOTE_SCHEMA)

Multi-page flow (for PDFs > BATCH_SIZE pages):
  1. DocumentLoader renders each page to PNG bytes.
  2. PageSplitter divides pages into batches (default 4 pages each).
     Leading "context" pages are prepended to later batches so the model
     still sees the document header on each call.
  3. The provider is called once per batch (sequential — no concurrency needed
     at this scale; Redis / Celery not required).
  4. ResultAggregator merges partial results: concatenates items, takes first
     non-empty scalar metadata, sums token usage.
  5. Post-processing coerces numeric fields, infers missing categories, etc.

Single-page or short docs (≤ BATCH_SIZE pages) skip batching — one API call.

Post-processing:
- coerces numeric fields (qty / unit_price / total_price)
- strips whitespace on strings
- best-effort category inference for tender items
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Callable

from PIL import Image

from apps.api.intelligence.aggregator import ResultAggregator
from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError, ContentModerationError,
)
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA
from apps.api.intelligence.splitter import PageSplitter

log = logging.getLogger(__name__)

# Used by category inference; matches apps/api/core/config.py ALL_CATEGORIES
KNOWN_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]

BATCH_SIZE = 4          # pages per LLM call
CONTEXT_PAGES = 1       # leading pages repeated on tail batches for header context
ProgressCallback = Callable[[str, int], None]


class ExtractionPipeline:
    """Coordinates document loading, batched LLM calls, and structured post-processing."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    # ─── public API ───────────────────────────────────────────────────────
    def extract_tender(
        self,
        file_path: str,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        _notify(progress_cb, "渲染PDF", 10)
        images = DocumentLoader.to_images(file_path)
        resp = self._run_batched(images, TENDER_SCHEMA, TENDER_PROMPT, "tender", progress_cb)
        _notify(progress_cb, "整理结果", 95)
        resp.data = self._postprocess_tender(resp.data)
        return resp

    def extract_quote(
        self,
        file_path: str,
        context: dict[str, Any] | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        _notify(progress_cb, "渲染PDF", 10)
        images = DocumentLoader.to_images(file_path)
        resp = self._run_batched(images, QUOTE_SCHEMA, QUOTE_PROMPT, "quote", progress_cb)
        _notify(progress_cb, "整理结果", 95)
        resp.data = self._postprocess_quote(resp.data, context or {})
        return resp

    # ─── batched execution ─────────────────────────────────────────────────
    def _run_batched(
        self,
        images: list[bytes],
        schema: dict,
        prompt: str,
        doc_type: str,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        """Split pages into batches, call provider once per batch, then aggregate."""
        _notify(progress_cb, "拆分页面", 15)
        batches = PageSplitter.split(images, batch_size=BATCH_SIZE, context_pages=CONTEXT_PAGES)
        n = len(batches)

        if n == 1:
            log.debug("Single batch (%d pages) — direct provider call", len(batches[0]))
            _notify(progress_cb, "识别第 1/1 批", 25)
            resp = self.provider.extract(batches[0], schema, prompt)
            _notify(progress_cb, "识别完成", 90)
            return resp

        log.info(
            "Multi-batch extraction: %d pages → %d batches (batch_size=%d)",
            len(images), n, BATCH_SIZE,
        )
        partials: list[ExtractionResponse] = []
        skipped_batches: list[int] = []          # 1-based indices of content-blocked batches

        for i, batch in enumerate(batches):
            log.debug("Batch %d/%d — %d pages", i + 1, n, len(batch))
            start_pct = 20 + int((i / n) * 65)
            done_pct = 20 + int(((i + 1) / n) * 65)
            _notify(progress_cb, f"识别第 {i + 1}/{n} 批", start_pct)
            try:
                partial = self.provider.extract(batch, schema, prompt)
            except ContentModerationError as e:
                # Batch has at least one flagged page — fall back to page-by-page
                # retry so we can salvage the OK pages and only skip the bad ones.
                log.warning(
                    "Batch %d/%d hit content moderation — retrying page-by-page: %s",
                    i + 1, n, e,
                )
                page_partials = self._retry_pages_individually(
                    batch, schema, prompt, batch_idx=i,
                    skipped_batches=skipped_batches,
                )
                partials.extend(page_partials)
                continue

            partials.append(partial)
            _notify(progress_cb, f"已完成第 {i + 1}/{n} 批", done_pct)
            items_found = len((partial.data or {}).get("items") or [])
            log.debug(
                "Batch %d/%d done — %d items, %d tokens",
                i + 1, n, items_found, partial.tokens_used or 0,
            )

        if not partials:
            raise ProviderError(
                f"All {n} batches were blocked by content moderation. "
                "Consider reducing RENDER_SCALE or inspecting the PDF pages."
            )

        _notify(progress_cb, "合并识别结果", 88)
        merged = ResultAggregator.merge(partials, doc_type)
        if skipped_batches:
            merged.metadata["skipped_batches"] = skipped_batches
            log.warning(
                "Extraction completed with %d/%d batches skipped (content moderation): %s",
                len(skipped_batches), n, skipped_batches,
            )
        log.info(
            "Aggregated %d/%d batches → %d total items, %d total tokens",
            len(partials), n,
            len((merged.data or {}).get("items") or []),
            merged.tokens_used or 0,
        )
        return merged

    # ─── per-page fallback ────────────────────────────────────────────────
    def _retry_pages_individually(
        self,
        batch: list[bytes],
        schema: dict,
        prompt: str,
        batch_idx: int,
        skipped_batches: list[int],
    ) -> list[ExtractionResponse]:
        """Retry a content-moderation-blocked batch one page at a time.

        Context pages (leading pages prepended for header context on tail batches)
        are skipped — they were already extracted in batch 0 and don't contain
        new item data.  Only the real content pages are retried individually.

        Returns a list of per-page ExtractionResponse objects (may be empty if
        every page in the batch is blocked).
        """
        # Tail batches (idx > 0) have CONTEXT_PAGES leading context pages
        # that should not be re-extracted.
        content_start = CONTEXT_PAGES if batch_idx > 0 else 0
        content_pages = batch[content_start:]

        page_partials: list[ExtractionResponse] = []
        for j, page_bytes in enumerate(content_pages):
            page_label = f"batch{batch_idx + 1}/page{content_start + j}"
            try:
                partial = self.provider.extract([page_bytes], schema, prompt)
                page_partials.append(partial)
                items_found = len((partial.data or {}).get("items") or [])
                log.debug(
                    "%s: OK — %d items, %d tokens",
                    page_label, items_found, partial.tokens_used or 0,
                )
            except ContentModerationError:
                log.warning(
                    "%s: blocked — retrying as left/right halves", page_label
                )
                # Level 3: split the single page into left and right halves.
                # Empirically, the content moderation triggers on the full-page
                # combination (dense table + seal overlay) but each half passes
                # individually. The data-bearing columns are in the left portion.
                half_partials = self._retry_page_as_halves(
                    page_bytes, schema, prompt, page_label, skipped_batches
                )
                page_partials.extend(half_partials)

        return page_partials

    def _retry_page_as_halves(
        self,
        page_bytes: bytes,
        schema: dict,
        prompt: str,
        page_label: str,
        skipped_batches: list,
    ) -> list[ExtractionResponse]:
        """Split a moderation-blocked page into left/right halves and retry each.

        Landscape PDF tables tend to have data in the left ~50% with the right
        side being whitespace or the stamp area — so left half usually captures
        everything useful.
        """
        with Image.open(io.BytesIO(page_bytes)) as img:
            w, h = img.size
            halves = {
                "left":  img.crop((0,     0, w // 2, h)),
                "right": img.crop((w // 2, 0, w,     h)),
            }
            half_bytes = {}
            for side, half_img in halves.items():
                buf = io.BytesIO()
                half_img.save(buf, format="PNG", optimize=True)
                half_bytes[side] = buf.getvalue()

        results: list[ExtractionResponse] = []
        for side, data in half_bytes.items():
            label = f"{page_label}/{side}"
            try:
                partial = self.provider.extract([data], schema, prompt)
                items_found = len((partial.data or {}).get("items") or [])
                log.debug("%s: OK — %d items", label, items_found)
                results.append(partial)
            except ContentModerationError:
                log.warning("%s: still blocked — giving up on this half", label)
                skipped_batches.append(f"{label}(blocked)")

        return results

    # ─── post-processing ──────────────────────────────────────────────────
    @staticmethod
    def _postprocess_tender(data: dict) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = (it.get("name") or "").strip()
            if not name:
                continue
            category = (it.get("category") or "").strip()
            if not category:
                category = _infer_category(name)
            cleaned.append({
                "name": name,
                "category": category,
                "spec": (it.get("spec") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "quantity": _coerce_num(it.get("quantity")),
                "remark": (it.get("remark") or "").strip(),
            })
        return {
            "project_name": (data.get("project_name") or "").strip(),
            "project_code": (data.get("project_code") or "").strip(),
            "tender_date": (data.get("tender_date") or "").strip(),
            "deadline": (data.get("deadline") or "").strip(),
            "items": cleaned,
        }

    @staticmethod
    def _postprocess_quote(data: dict, ctx: dict[str, Any]) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            material = (it.get("material") or "").strip()
            if not material:
                continue
            price = _coerce_num(it.get("unit_price"))
            qty = _coerce_num(it.get("qty"))
            total = _coerce_num(it.get("total_price"))
            if total is None and price is not None and qty is not None:
                total = round(price * qty, 4)
            cleaned.append({
                "material": material,
                "spec": (it.get("spec") or "").strip(),
                "brand": (it.get("brand") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "qty": qty,
                "unit_price": price,
                "unit_price_excl_tax": _coerce_num(it.get("unit_price_excl_tax")),
                "total_price": total,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "remark": (it.get("remark") or "").strip(),
            })
        return {
            "supplier_name": (data.get("supplier_name") or "").strip(),
            "quote_date": (data.get("quote_date") or "").strip(),
            "items": cleaned,
            "context": ctx,
        }


# ─── helpers ──────────────────────────────────────────────────────────────
def _coerce_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in {".", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _infer_category(name: str) -> str:
    """Heuristic: scan material name for a known category keyword."""
    for cat in KNOWN_CATEGORIES:
        if cat in name:
            return cat
    return ""


def _notify(progress_cb: ProgressCallback | None, stage: str, pct: int) -> None:
    if progress_cb:
        progress_cb(stage, max(0, min(100, pct)))
