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

from apps.api.core.config import get_settings
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
    "桥架", "母线槽", "配电箱", "电缆", "阀门", "不锈钢管",
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

        # 招标（比价）与邀标对**招标文件解析能力的要求是一致的**：都要采购清单，
        # 也都要封面四标量。故两个入口共用 `parse_tender_document`，只在输出映射上
        # 不同——本方法映射成 TENDER_SCHEMA，`extract_tender_bidlist` 映射成
        # TenderAnchor。给两条流程各写一个解析器，同一份 PDF 迟早会给出两种清单。
        if hasattr(self.provider, "vl_extract_csv"):
            from apps.api.intelligence.vl_tender import parse_tender_document

            s = get_settings()
            parsed = parse_tender_document(
                file_path,
                vl_call=lambda imgs, prompt: self.provider.vl_extract_csv(
                    imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL),
                orient_call=lambda parts, prompt: self.provider.vl_extract_csv(
                    [b for _t, b in parts], prompt,
                    model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
                progress_cb=progress_cb,
            )
            return self._tender_draft_to_response(parsed, t_start)

        log.warning(
            "provider %s 没有 vl_extract_csv，招标文件走逐页批量兜底。",
            type(self.provider).__name__,
        )
        images = DocumentLoader.to_images(file_path)
        resp = self._run_batched(images, TENDER_SCHEMA, TENDER_PROMPT, "tender", progress_cb)
        _notify(progress_cb, "整理结果", 95)
        resp.data = self._postprocess_tender(resp.data)
        self._log_extraction("tender", file_path, images, resp, t_start)
        return resp

    def _tender_draft_to_response(self, parsed, t_start: float) -> ExtractionResponse:
        """TenderParseResult → TENDER_SCHEMA 形状的 ExtractionResponse。

        与 `extract_tender_bidlist` 读的是**同一个 draft**，只是取的字段不同：
        这里要 TENDER_SCHEMA 的 name/category/spec/unit/quantity/remark，
        那边要 TenderAnchor 的 seq/pressure/materials/canonical。
        """
        draft = parsed.draft
        items = [
            {
                "name": r.fields.get("name") or "",
                # 品类由 _postprocess_tender 的既有推断补齐，识别侧不猜
                "category": "",
                "spec": r.fields.get("spec") or "",
                "unit": r.fields.get("unit") or "",
                "quantity": r.fields.get("qty"),
                "remark": r.fields.get("remark") or "",
                # 未落槽位的列原样带出——换个品类（桥架的表面处理等）全靠它
                "extended_attrs": dict(r.extra_fields or {}),
            }
            for r in draft.rows if r.row_type == "quote_line"
        ]
        data = {**parsed.meta, "items": items}
        resp = ExtractionResponse(
            data=self._postprocess_tender(data),
            raw_text="", confidence=1.0, tokens_used=0,
            provider=getattr(self.provider, "name", ""),
            duration_ms=int((time.time() - t_start) * 1000),
            metadata={
                "doc_type": "tender",
                "recognizer": "vl_direct",
                "quality_status": draft.quality.status,
                "quality_blocking_reasons": list(draft.quality.blocking_reasons or []),
                "row_ledger": draft.ledger.to_dict() if draft.ledger else None,
                "rotations": parsed.rotations,
                "orientation_unresolved": parsed.unresolved_pages,
            },
        )
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

        # 报价识别 = VL-direct。整份页面图像 → 视觉模型 → CSV → ExtractionDraft。
        #
        # **legacy 的报价分支（OCR → HTML → TableGrid → LLM）已于 2026-08-10 归档**
        # （docs/design/21）：`recognize_tables` 仍然存在，但报价侧不再调用它——
        # 它现在只服务招标清单（services/tender_pdf.py），那一侧尚无 VL 实现。
        # 不要为了"多一条保险"把它接回来：两条路并存意味着任何一次识别结果都要先问
        # "这是哪条路出来的"，而实测 provider 缺一个方法就会静默换路且无人察觉。
        if hasattr(self.provider, "vl_extract_csv"):
            from apps.api.intelligence.vl_direct import recognize_quote_vl
            s = get_settings()
            draft = recognize_quote_vl(
                file_path,
                vl_call=lambda imgs, prompt: self.provider.vl_extract_csv(
                    imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL),
                orient_call=lambda parts, prompt: self.provider.vl_extract_csv(
                    [b for _t, b in parts], prompt,
                    model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
                progress_cb=progress_cb,
            )
            return self._draft_to_quote_response(draft, context or {}, t_start)

        # provider 连多图调用都不具备（自定义 mock、非 dashscope 后端）→ 逐页批量。
        # 这不是 legacy 识别链路，只是"没有 VL 能力时还能出点东西"的兜底。
        log.warning(
            "provider %s 没有 vl_extract_csv，报价走逐页批量兜底而非 VL-direct。",
            type(self.provider).__name__,
        )
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
                # 行位证据：顺序直连按位置对齐锚点要用，定向重读要用它回到具体页。
                # 缺了它，`_doc_order` 的三态逻辑拿不到输入，只能退回载入顺序。
                "document_row_index": f.get("document_row_index"),
                "page_row_index": f.get("page_row_index"),
                "source_page": row.source_ref.page or None,
                "copy_no": f.get("copy_no") or "",
                # 「原文明确不报价」——合法事实，与"读不到"分开，入库门据此不阻断
                "not_quoted": bool(f.get("not_quoted")),
            })
        data_in = {
            "supplier_name": (draft.meta or {}).get("supplier_name") or "",
            "items": items,
        }
        processed = self._postprocess_quote(data_in, context)
        # 声明总价核对门的输入（quote_confirmation_service._build_checksum 读
        # job.result["_doc_meta"]，由 document_ingestion.py:236 从这里搬运）。
        # 此前 VL 路径从不产出这个键——门本身逻辑是对的，只是从未接到过输入，
        # 生产上对任何 PDF 报价都判 unknown、不阻断（docs/design/21 §2.2/§2.3）。
        quote_meta = (draft.meta or {}).get("quote_meta")
        resp = ExtractionResponse(
            data=processed,
            metadata={
                "doc_type": "quote",
                "quality_status": draft.quality.status,
                "quality_blocking_reasons": draft.quality.blocking_reasons,
                "page_count": draft.page_count,
                "target_pages": draft.target_pages,
                # 行数守恒台账（doc/19 §L3）：丢行必须随结果一起暴露，
                # 否则调用方拿到的 200 无法区分"这份文档只有这些行"和"我们丢了行"。
                "row_ledger": draft.ledger.to_dict() if draft.ledger else None,
                **({"doc_meta": quote_meta} if quote_meta else {}),
            },
            tokens_used=0,
            duration_ms=int((_time.time() - t_start) * 1000),
        )
        _ledger = draft.ledger
        log.info(
            "draft_to_quote_response: quality=%s items=%d supplier=%r ledger=%s",
            draft.quality.status,
            len(processed.get("items") or []),
            processed.get("supplier_name"),
            (f"{_ledger.recognized_rows}/{_ledger.expected_rows} rows, "
             f"{len(_ledger.empty_pages)} empty pages") if _ledger else "n/a",
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
            # 「原文明确不报价」的标记必须在这里就固化下来。_coerce_num 把「/」「无」
            # 「N/A」和空白一律变成 None，**两种语义就此不可分辨**——下游只能把合法的
            # 不报价行当成缺陷，422 逼用户编一个金额。故在还看得到原始文本的这一层
            # 判定一次，用布尔标记随行带走。
            from apps.api.services.draft_integrity import (
                AMOUNT_NOT_QUOTED, classify_amount_cell,
            )
            not_quoted = any(
                classify_amount_cell(it.get(k)) == AMOUNT_NOT_QUOTED
                for k in ("total_price", "total_price_incl_tax", "total_price_excl_tax")
            )
            # 不在识别阶段派生合价。上游一旦把 qty×price 填进 total_price，
            # 下游 confirm 就无法区分"原文读到的"和"系统算的"，会误标成 ocr。
            # 只给候选值供前端提示，权威字段保持 None（doc/19 §L2）。
            derived_candidate = None
            if total is None and price is not None and qty is not None:
                derived_candidate = round(price * qty, 4)
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
                "derived_total_candidate": derived_candidate,
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
                "derived_total_candidate": derived_candidate,
                # 原文明确不报价（「/」「无」「N/A」…）。与"读不到"分开，见上方注释。
                "not_quoted": not_quoted,
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
                # 行位证据。**这里是白名单重建，不加就会丢** —— 丢了之后顺序直连
                # 只能退回载入顺序，定向重读也回不到具体页。
                "source_page": it.get("source_page"),
                "page_row_index": it.get("page_row_index"),
                "copy_no": it.get("copy_no") or "",
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


