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
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from apps.api.intelligence.aggregator import ResultAggregator
from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError, ContentModerationError,
)
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA
from apps.api.intelligence.quote_fact import build_canonical, apply_arithmetic_validation
from apps.api.intelligence.price_basis import derive_price_basis

log = logging.getLogger(__name__)
extraction_log = logging.getLogger("mempas.extraction")

# Used by category inference; matches apps/api/core/config.py ALL_CATEGORIES
KNOWN_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]

BATCH_SIZE = 1          # legacy export; extraction now uses one page per call
# 每份文档内的页级并发（OCR/LLM）。报价一般 5~10 页，默认 5。env 可调。
# 单一来源：table_recognizer 从此处 import，避免两处硬编码漂移。
PAGE_CONCURRENCY = max(1, int(os.getenv("PAGE_CONCURRENCY", "5")))
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
        t_start = time.time()
        images = DocumentLoader.to_images(file_path)
        resp = self._run_batched(images, TENDER_SCHEMA, TENDER_PROMPT, "tender", progress_cb)
        _notify(progress_cb, "整理结果", 95)
        resp.data = self._postprocess_tender(resp.data)
        self._log_extraction("tender", file_path, images, resp, t_start)
        return resp

    def extract_tender_bidlist(
        self,
        file_path: str,
        progress_cb: ProgressCallback | None = None,
        bidlist_pages: list[int] | None = None,
        brand_page: int | None = None,
    ) -> dict[str, Any]:
        """招标文件 PDF → 投标清单锚点 + 品牌映射（返回 dict，非 ExtractionResponse）。

        委托给 services.tender_pdf；复用本 pipeline 的 provider（OCR 能力）。
        """
        from apps.api.services.tender_pdf import extract_bidlist
        return extract_bidlist(
            file_path, self.provider, progress_cb=progress_cb,
            bidlist_pages=bidlist_pages, brand_page=brand_page,
        )

    def extract_quote(
        self,
        file_path: str,
        context: dict[str, Any] | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ExtractionResponse:
        _notify(progress_cb, "渲染PDF", 10)
        t_start = time.time()

        if (hasattr(self.provider, "ocr_pages_with_roles")
                and hasattr(self.provider, "_llm_call_json")):
            # Unified skeleton path: full-page OCR → role classify → detect →
            # per-page LLM (thinking retry + adaptive tiling) → quality gate
            from apps.api.intelligence.table_recognizer import recognize_tables
            draft = recognize_tables(
                file_path=file_path,
                provider=self.provider,
                adapter=_get_quote_adapter(),
                progress_cb=progress_cb,
            )
            resp = self._draft_to_quote_response(draft, context or {}, t_start)
        else:
            # Legacy path for providers without OCR (mock, non-dashscope)
            images = DocumentLoader.to_images(file_path)
            resp = self._run_batched(images, QUOTE_SCHEMA, QUOTE_PROMPT, "quote", progress_cb)
            _notify(progress_cb, "整理结果", 95)
            resp.data = self._postprocess_quote(resp.data, context or {})
            self._log_extraction("quote", file_path, images, resp, t_start)

        return resp

    def _draft_to_quote_response(
        self,
        draft: Any,
        context: dict,
        t_start: float,
    ) -> ExtractionResponse:
        """Convert ExtractionDraft → ExtractionResponse (quote side)."""
        import time as _time
        # Convert DraftRow → postprocess-compatible item dicts
        items = []
        for row in draft.rows:
            if row.row_type not in ("quote_line",):
                continue
            f = row.fields
            items.append({
                "material": f.get("name") or "",
                "spec": f.get("spec") or "",
                "brand": f.get("brand") or "",
                "unit": f.get("unit") or "",
                "qty": f.get("qty"),
                # 全部原始价格字段（含税/不含税/通用），口径桥接与入库均需要，不得丢失
                "unit_price": f.get("unit_price"),
                "unit_price_incl_tax": f.get("unit_price_incl_tax"),
                "unit_price_excl_tax": f.get("unit_price_excl_tax"),
                "total_price": f.get("total_price"),
                "total_price_incl_tax": f.get("total_price_incl_tax"),
                "total_price_excl_tax": f.get("total_price_excl_tax"),
                "tax_rate": f.get("tax_rate"),
                "tax_amount": f.get("tax_amount"),
                "material_type": f.get("material_type") or "",
                "remark": f.get("remark") or "",
                "canonical": f.get("canonical") or {},
                "normalized_material": f.get("normalized_material") or "",
                "ocr_correction_reason": f.get("ocr_correction_reason") or "",
                "source_ref": row.source_ref.to_dict(),
                # 算术校验审计：原值 qty 不改，suggested_qty 仅参考；validation_flags 完整传递
                "validation_flags": list(row.validation_flags or []),
                "raw_qty": f.get("qty"),
                "suggested_qty": f.get("arith_suggested_qty"),
            })
        data_in = {
            "supplier_name": (draft.meta or {}).get("supplier_name") or "",
            "items": items,
        }
        processed = self._postprocess_quote(data_in, context)
        resp = ExtractionResponse(
            data=processed,
            metadata={
                "doc_type": "quote",
                "quality_status": draft.quality.status,
                "quality_blocking_reasons": draft.quality.blocking_reasons,
                "page_count": draft.page_count,
                "target_pages": draft.target_pages,
            },
            tokens_used=0,
            duration_ms=int((_time.time() - t_start) * 1000),
        )
        log.info(
            "draft_to_quote_response: quality=%s items=%d supplier=%r",
            draft.quality.status,
            len(processed.get("items") or []),
            processed.get("supplier_name"),
        )
        return resp

    # ─── batched execution (legacy / non-OCR-provider path) ──────────────
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

        # ── Supplier name cover fallback ───────────────────────────────────
        # If aggregation still couldn't find a company name (e.g. every page only
        # had a product brand), re-OCR the first 1-2 cover pages with a targeted
        # prompt to recover the supplier name before postprocessing.
        if (doc_type == "quote"
                and not (merged.data or {}).get("supplier_name")
                and images
                and hasattr(self.provider, "extract_supplier_name_from_cover")):
            _notify(progress_cb, "封面补充供应商名", 89)
            # Scan front pages (bidder name may be buried on the stamped 投标单位名称
            # page, deeper than the cover which often shows only the 招标人/buyer).
            name = self.provider.extract_supplier_name_from_cover(images[:10])
            if name:
                merged.data["supplier_name"] = name

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

    def _log_extraction(
        self,
        doc_type: str,
        file_path: str,
        images: list[bytes],
        resp: ExtractionResponse,
        t_start: float,
    ) -> None:
        """Emit a structured JSON log line for every extraction run."""
        items = (resp.data or {}).get("items") or []
        skipped = (resp.metadata or {}).get("skipped_pages") or []
        record = {
            "type": doc_type,
            "file": Path(file_path).name,
            "provider": getattr(self.provider, "name", "unknown"),
            "model": getattr(self.provider, "model", "unknown"),
            "pages": len(images),
            "concurrency": PAGE_CONCURRENCY,
            "items": len(items),
            "tokens": resp.tokens_used or 0,
            "skipped": len(skipped),
            "skipped_detail": skipped or None,
            "duration_s": round(time.time() - t_start, 1),
            "duration_llm_ms": resp.duration_ms,
        }
        extraction_log.info(json.dumps(record, ensure_ascii=False))

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
            ext = it.get("extended_attrs")
            if not isinstance(ext, dict):
                ext = {}
            ext = {k: v for k, v in ext.items() if v is not None and v != ""}
            cleaned.append({
                "name": name,
                "category": category,
                "spec": (it.get("spec") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "quantity": _coerce_num(it.get("quantity")),
                "remark": (it.get("remark") or "").strip(),
                "extended_attrs": ext,
            })
        return {
            "project_name": (data.get("project_name") or "").strip(),
            "project_code": (data.get("project_code") or "").strip(),
            "tender_date": (data.get("tender_date") or "").strip(),
            "deadline": (data.get("deadline") or "").strip(),
            "items": cleaned,
        }

    @staticmethod
    def _validate_items(items: list[dict]) -> list[dict]:
        """Thin delegate to the shared quote_fact.apply_arithmetic_validation.

        Kept for backward compat; logic lives in quote_fact.py so the tabular
        ingestion path can share it without importing this module.
        """
        return apply_arithmetic_validation(items)

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
            # Rows flagged by _assign_source_ref_from_grids as not a quote_line
            # (grand_total, subtotal, etc.) must not enter as regular quote items
            if it.get("source_ref_invalid"):
                continue
            price = _coerce_num(it.get("unit_price"))
            qty = _coerce_num(it.get("qty"))
            total = _coerce_num(it.get("total_price"))
            if total is None and price is not None and qty is not None:
                total = round(price * qty, 4)
            spec_str = (it.get("spec") or "").strip()
            material_type = (it.get("material_type") or "").strip()
            # Layer 1: OCR correction fields from LLM (raw text stays in material)
            norm_material = (it.get("normalized_material") or "").strip()
            ocr_reason = (it.get("ocr_correction_reason") or "").strip()

            # Canonical: merge code extraction with LLM result (LLM overrides).
            # normalized_material takes priority over raw material for code extraction
            # so 形近字 errors don't corrupt canonical.valve_type and kill matching.
            canonical: dict = build_canonical(
                material, spec_str, material_type=material_type,
                llm_canonical=it.get("canonical"),
                normalized_material=norm_material,
            )

            # ── 价格口径桥接（§4/§9）：判定 price_basis + effective 价格，原值不改 ──
            unit_incl = _coerce_num(it.get("unit_price_incl_tax"))
            unit_excl = _coerce_num(it.get("unit_price_excl_tax"))
            total_incl = _coerce_num(it.get("total_price_incl_tax"))
            total_excl = _coerce_num(it.get("total_price_excl_tax"))
            basis_info = derive_price_basis({
                "qty": qty,
                "unit_price": price,
                "unit_price_incl_tax": unit_incl,
                "unit_price_excl_tax": unit_excl,
                "total_price": total,
                "total_price_incl_tax": total_incl,
                "total_price_excl_tax": total_excl,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "tax_amount": _coerce_num(it.get("tax_amount")),
            })

            cleaned.append({
                "material": material,
                "spec": spec_str,
                "brand": (it.get("brand") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "qty": qty,
                "unit_price": price,
                "unit_price_incl_tax": unit_incl,
                "unit_price_excl_tax": unit_excl,
                "total_price": total,
                "total_price_incl_tax": total_incl,
                "total_price_excl_tax": total_excl,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "tax_amount": _coerce_num(it.get("tax_amount")),
                # 比价口径桥接结果
                "price_basis": basis_info["price_basis"],
                "effective_unit_price": basis_info["effective_unit_price"],
                "effective_total_price": basis_info["effective_total_price"],
                "effective_unit_recovered": basis_info.get("effective_unit_recovered", False),
                "material_type": material_type,
                "remark": (it.get("remark") or "").strip(),
                "canonical": canonical,
                "validation_warning": "",
                "validation_flags": list(it.get("validation_flags") or []),
                "raw_qty": _coerce_num(it.get("raw_qty")) if it.get("raw_qty") is not None else qty,
                "suggested_qty": _coerce_num(it.get("suggested_qty")),
                "normalized_material": norm_material,
                "ocr_correction_reason": ocr_reason,
                "source_ref": it.get("source_ref"),  # {page, table, row} from TableGrid
            })
        apply_arithmetic_validation(cleaned)
        # 全局文档行序（1..N，按 page→table→row 的抽取顺序）。顺序直连对齐用它做行身份，
        # 不依赖会跨页重置的 source_ref.row 或数据库自增 id。
        for _i, _it in enumerate(cleaned, 1):
            _it["document_row_index"] = _i
        return {
            "supplier_name": (data.get("supplier_name") or "").strip(),
            "quote_date": (data.get("quote_date") or "").strip(),
            "items": cleaned,
            "context": ctx,
        }


# ─── Kept for backward compat (tests import this directly) ───────────────────

def _assign_source_ref_from_grids(items: list[dict], table_grids: list) -> None:
    """Assign source_ref from TableGrid row indices (legacy utility).

    Used by tests. Production path now goes through table_recognizer.
    """
    page = table_grids[0].page if table_grids else None
    valid_pairs: set[tuple[int, int]] = {
        (grid.table_index, row.row_index)
        for grid in table_grids
        for row in grid.rows
        if row.row_type == "quote_line"
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        t_idx = item.pop("table_index", None)
        r_idx = item.pop("row_index", None)
        if r_idx is None:
            continue
        try:
            t = int(t_idx) if t_idx is not None else 0
            r = int(r_idx)
        except (ValueError, TypeError):
            item["source_ref_invalid"] = f"non-integer table={t_idx!r} row={r_idx!r}"
            continue
        ref: dict = {"page": page, "table": t, "row": r}
        if valid_pairs and (t, r) not in valid_pairs:
            ref["valid"] = False
            item["source_ref_invalid"] = f"index ({t},{r}) not in quote_lines"
        item["source_ref"] = ref


# ─── QuoteAdapter（报价侧 RecognizeAdapter）────────────────────────────────

def _quote_detect_pages(htmls: list[str]) -> list[int]:
    from apps.api.intelligence.page_classifier import classify_page, PageRole
    return [
        i + 1
        for i, html in enumerate(htmls)
        if classify_page(html).primary_role in (PageRole.QUOTE_TABLE, PageRole.UNKNOWN)
    ]


def _quote_extract_meta(non_target_htmls: list[tuple], provider: Any) -> dict:
    from apps.api.intelligence.page_classifier import classify_page, PageRole
    from apps.api.intelligence.providers.dashscope_ocr import MAX_META_PAGES

    meta_htmls = [
        html for _page_no, html in non_target_htmls
        if classify_page(html).primary_role in (PageRole.COVER, PageRole.SUMMARY, PageRole.OTHER)
    ][:MAX_META_PAGES]

    supplier_name = ""
    declared_total = None

    if meta_htmls and hasattr(provider, "extract_doc_meta"):
        try:
            doc_meta = provider.extract_doc_meta(meta_htmls)
            supplier_name = doc_meta.get("supplier_name") or ""
            declared_total = doc_meta.get("bid_total") or doc_meta.get("declared_total")
        except Exception as exc:
            log.warning("quote extract_meta: extract_doc_meta failed: %s", exc)

    return {"supplier_name": supplier_name, "declared_total": declared_total}


def _quote_prompt_for_mode(input_mode: str) -> str:
    from apps.api.intelligence.providers.dashscope_ocr import (
        _QUOTE_S2_TABLE_PROMPT, _QUOTE_S2_PROMPT,
    )
    if input_mode == "table_grid":
        return _QUOTE_S2_TABLE_PROMPT
    return _QUOTE_S2_PROMPT


_QUOTE_ADAPTER = None  # deferred import to avoid circular — set at first use


def _get_quote_adapter():
    """Return the QuoteAdapter singleton (lazy to avoid import cycles)."""
    global _QUOTE_ADAPTER
    if _QUOTE_ADAPTER is None:
        from apps.api.intelligence.table_recognizer import RecognizeAdapter
        from apps.api.intelligence.providers.dashscope_ocr import _QUOTE_S2_PROMPT
        _QUOTE_ADAPTER = RecognizeAdapter(
            doc_type="quote",
            detect_pages=_quote_detect_pages,
            row_prompt=_QUOTE_S2_PROMPT,
            name_key="material",
            extract_meta=_quote_extract_meta,
            prompt_for_mode=_quote_prompt_for_mode,
        )
    return _QUOTE_ADAPTER


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


