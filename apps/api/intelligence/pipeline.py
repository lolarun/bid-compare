"""ExtractionPipeline — orchestrates loader → provider → aggregator → post-processing.

Two public methods:
- extract_tender(file_path) → ExtractionResponse (data matches TENDER_SCHEMA)
- extract_quote(file_path, context) → ExtractionResponse (data matches QUOTE_SCHEMA)

Multi-page flow:
  1. DocumentLoader renders each page to PNG bytes.
  2. Each page is sent to the provider as its own task.
  3. Up to PAGE_CONCURRENCY pages are recognised concurrently.
  4. ResultAggregator merges partial results in page order: concatenates items, takes first
     non-empty scalar metadata, sums token usage.
  5. Post-processing coerces numeric fields, infers missing categories, etc.

Post-processing:
- coerces numeric fields (qty / unit_price / total_price)
- strips whitespace on strings
- best-effort category inference for tender items
"""

from __future__ import annotations

import io
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from PIL import Image

from apps.api.intelligence.aggregator import ResultAggregator
from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError, ContentModerationError,
)
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA

log = logging.getLogger(__name__)

# Used by category inference; matches apps/api/core/config.py ALL_CATEGORIES
KNOWN_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]

BATCH_SIZE = 1          # legacy export; extraction now uses one page per call
PAGE_CONCURRENCY = 10   # max concurrent page-level LLM calls
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
        """Recognise pages concurrently, then aggregate partials in page order."""
        _notify(progress_cb, "拆分页面", 15)
        n = len(images)
        if n == 0:
            raise ProviderError("Document produced no pages for extraction")

        if n == 1:
            log.debug("Single page — direct provider call")
            _notify(progress_cb, "识别第 1/1 页", 25)
            resp = self.provider.extract([images[0]], schema, prompt)
            _notify(progress_cb, "识别完成", 90)
            return resp

        workers = min(PAGE_CONCURRENCY, n)
        t0 = time.time()
        log.info(
            "Page-level extraction: %d pages with concurrency=%d",
            n, workers,
        )
        _notify(progress_cb, f"并发识别 {n} 页（最多 {workers} 页同时）", 20)

        partials_by_page: dict[int, list[ExtractionResponse]] = {}
        skipped_pages: list[str] = []
        completed = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._extract_page, idx, image, schema, prompt): idx
                for idx, image in enumerate(images)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    page_partials, page_skips = future.result()
                except Exception as e:
                    log.warning("Page %d/%d failed: %s", idx + 1, n, e)
                    skipped_pages.append(f"page{idx + 1}({type(e).__name__}: {e})")
                    page_partials = []
                    page_skips = []

                if page_partials:
                    partials_by_page[idx] = page_partials
                skipped_pages.extend(page_skips)
                completed += 1
                done_pct = 20 + int((completed / n) * 65)
                _notify(progress_cb, f"已完成第 {completed}/{n} 页", done_pct)
                items_found = sum(len((p.data or {}).get("items") or []) for p in page_partials)
                log.debug(
                    "Page %d/%d done — %d items from %d partials",
                    idx + 1, n, items_found, len(page_partials),
                )

        partials: list[ExtractionResponse] = []
        for idx in sorted(partials_by_page):
            partials.extend(partials_by_page[idx])

        if not partials:
            raise ProviderError(
                f"All {n} pages failed or were blocked by content moderation. "
                "Consider reducing RENDER_SCALE or inspecting the PDF pages."
            )

        _notify(progress_cb, "合并识别结果", 88)
        merged = ResultAggregator.merge(partials, doc_type)
        merged.duration_ms = int((time.time() - t0) * 1000)
        if skipped_pages:
            merged.metadata["skipped_pages"] = skipped_pages
            log.warning(
                "Extraction completed with %d/%d pages skipped/failed: %s",
                len(skipped_pages), n, skipped_pages,
            )
        log.info(
            "Aggregated %d page partials from %d/%d pages → %d total items, %d total tokens",
            len(partials), len(partials_by_page), n,
            len((merged.data or {}).get("items") or []),
            merged.tokens_used or 0,
        )
        return merged

    def _extract_page(
        self,
        page_idx: int,
        page_bytes: bytes,
        schema: dict,
        prompt: str,
    ) -> tuple[list[ExtractionResponse], list[str]]:
        """Extract a single page, with half-page fallback for moderation blocks."""
        try:
            return [self.provider.extract([page_bytes], schema, prompt)], []
        except ContentModerationError:
            page_label = f"page{page_idx + 1}"
            log.warning("%s: blocked — retrying as left/right halves", page_label)
            skipped: list[str] = []
            return self._retry_page_as_halves(page_bytes, schema, prompt, page_label, skipped), skipped

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
