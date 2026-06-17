"""DashScopeOCRProvider — two-stage OCR+LLM extraction via Alibaba DashScope.

Stage 1: Qwen-VL-OCR (table_parsing) → HTML tables per page
Stage 2: qwen3.6-flash → structured JSON per page
Stage 2b (fallback): if supplier_name still empty after aggregation, OCR the cover
         page(s) and ask LLM specifically for the company name.

Config (apps/api/.env):
  DASHSCOPE_API_KEY    — single key (used when DASHSCOPE_API_KEYS is absent)
  DASHSCOPE_API_KEYS   — comma-separated key list for multi-key rotation
                          (e.g. sk-aaa,sk-bbb — up to N×6 concurrent pages)
  DASHSCOPE_BASE_URL   — default https://dashscope.aliyuncs.com/compatible-mode/v1
  DASHSCOPE_OCR_MODEL  — default qwen-vl-ocr-latest
  DASHSCOPE_LLM_MODEL  — default qwen3.6-flash
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import threading
import time
from typing import Any

import dashscope
import openai
from openai import OpenAI

from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError,
)

log = logging.getLogger(__name__)

_MAX_RETRIES = 5
_RETRY_DELAY = 3          # seconds; linear backoff: delay × attempt (3, 6, 9, 12, 15)
_PER_KEY_CONCURRENCY = 6  # max simultaneous API calls per key

# ── Stage 2 prompts (OCR HTML → structured JSON) ────────────────────────
_TENDER_S2_PROMPT = """你是机电材料招投标助理。下面是OCR识别出的HTML表格内容。
请从中提取采购材料清单，返回严格的JSON格式。

要求：
- 只提取材料/设备条目，不要表头、合计行、小计行
- 材料名称按原文，不要简化
- 品类从以下选项选择：桥架、母线槽、配电箱、阀门、不锈钢管、水箱、潜水泵、风口风阀、风机盘管、空调泵；无法判断留空
- 数量若为'若干'等非数字，留null
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "投标单位名称", "items": [{"name": "材料名称", "category": "品类", "spec": "规格型号", "unit": "单位", "quantity": 数量或null, "remark": "备注"}]}

如果该页没有材料清单（如封面、证书等非清单页），返回 {"items": []}"""

_QUOTE_S2_PROMPT = """你是机电材料报价单解析助理。下面是OCR识别出的HTML表格内容。
请从中提取报价明细，返回严格的JSON格式。

要求：
- 【完整性】逐行提取，不要遗漏任何一条材料报价行；表格有多少数据行就返回多少条
- 只提取材料报价行，不要表头、合计行、小计行
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）
- 总价若已标注使用原值，否则留null
- 税率用小数如0.13表示13%
- 品牌按原文
- 无法识别的字段返回空字符串或null

对于阀门类材料（截止阀/闸阀/止回阀/球阀/蝶阀/减压阀/疏水阀/过滤器等），
额外填写 canonical 对象：
- valve_type: 阀门类型，如"截止阀"（按原文）
- dn: 公称直径，格式"DN25"；Φ57/2寸/50mm 请转换
- pn: 公称压力，格式"PN16"；1.6MPa→PN16
- material: 主材质（不锈钢/铸铁/球墨铸铁等）
- connection: 连接方式（螺纹/法兰/焊接等）
非阀门类材料，canonical 留空对象 {}。

OCR 纠错（阀门类）：当你发现材料名称存在明显形近字 OCR 错误时（如"阀阀"→闸阀、"橡胶海"→橡胶瓣）：
- normalized_material: 纠错后的正确名称（确信时填，否则留空字符串）
- ocr_correction_reason: 纠错依据（词表命中+相邻行规格连续性），无纠错时留空字符串
合法词表：闸阀/截止阀/止回阀/球阀/蝶阀/橡胶瓣止回阀/节能消声止回阀/缓闭式止回阀/低阻力倒流防止器/倒流防止器/小阻力可调式减压阀组/减压阀组/Y型过滤器
material 字段仍按原文填写；normalized_material 仅在确认为OCR错别字时才填，不确定留空。

返回JSON格式：
{"supplier_name": "供应商名称", "items": [{"material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率小数, "material_type": "材质", "remark": "备注", "canonical": {}, "normalized_material": "", "ocr_correction_reason": ""}]}

如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""

_META_S2_PROMPT = """你是机电材料招投标助理。下面是投标文件封面/汇总页或营业执照/资质证书页的OCR HTML内容。
请提取元信息，只返回JSON：
{"supplier_name": "投标单位全称或空字符串", "bid_total": 投标总价数字或null, "bid_total_basis": "tax_included|tax_excluded|unknown", "tax_rate": 税率小数或null}
提取规则：
- supplier_name：优先从"投标单位"/"报价单位"/"投标人"字段取；若为营业执照页，从"名称"/"称"字段取公司全名（即使列标题因OCR截断只剩"称"）；不要填经销商授权书中的品牌商名称。
- 若该页没有相关信息，对应字段返回null或空字符串。"""

# Stage 2 prompt for structured TableGrid JSON input (replaces raw HTML when available)
_QUOTE_S2_TABLE_PROMPT = """你是机电材料报价单解析助理。以下是 OCR 识别后按页面表格整理的结构化数据（JSON 格式）。

请从所有 row_type="quote_line" 的行中提取每一条报价明细。每条明细必须包含该行的 table_index 和 row_index（直接从输入复制，不要修改）。
row_type 为 subtotal/grand_total/header/empty/note 的行忽略不提取。

要求：
- 【完整性】所有 row_type=quote_line 的行都要提取，一行不能遗漏
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）；只有一个价格时填到 unit_price
- 对阀门类材料（截止阀/闸阀/止回阀/球阀/蝶阀/减压阀/疏水阀/过滤器等）额外填写 canonical 对象：valve_type/dn/pn/material/connection
- material_type：若表格有独立材质列按原文填；否则从规格型号中提取；无则留空字符串
- 总价若表格已标注使用原值，否则留 null（不要自己计算）
- 税率用小数如 0.13 表示 13%
- supplier_name：若当前页面有明确供应商/投标单位名称则填，否则留空字符串
- 无法识别的字段返回空字符串或 null，不要猜测

OCR 纠错（阀门类）：当你发现材料名称存在明显形近字 OCR 错误时（如"阀阀"→闸阀、"橡胶海"→橡胶瓣）：
- normalized_material: 纠错后的正确名称（确信时填，否则留空字符串）
- ocr_correction_reason: 纠错依据（词表命中+相邻行规格连续性），无纠错时留空字符串
合法词表：闸阀/截止阀/止回阀/球阀/蝶阀/橡胶瓣止回阀/节能消声止回阀/缓闭式止回阀/低阻力倒流防止器/倒流防止器/小阻力可调式减压阀组/减压阀组/Y型过滤器
material 字段仍按原文填写；normalized_material 仅在确认为OCR错别字时才填，不确定留空。

返回 JSON 格式（table_index 和 row_index 必须包含）：
{"supplier_name": "供应商名称", "items": [{"table_index": 0, "row_index": 2, "material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率, "material_type": "材质", "remark": "备注", "canonical": {}, "normalized_material": "", "ocr_correction_reason": ""}]}

没有报价明细时返回 {"items": []}"""

# Limits replacing the old hard MAX_PAGES = 12
MAX_QUOTE_TABLE_PAGES = 30
MAX_META_PAGES = 5

# ── Cover-page supplier-name fallback prompt ─────────────────────────────
_SUPPLIER_NAME_PROMPT = """从以下HTML内容中，找出【投标人/投标单位（卖方报价方）的公司全称】。

投标文件里常出现4类公司，只要"投标人"，其余三类一律排除：
- 【投标人=要的】卖方/报价方。【必须】有明确标签："投标单位名称""投标人""（盖章）投标单位"或报价单落款盖章处。
- 【招标人=排除】买方/甲方/采购方。出现在封面顶部、"致：XX公司"（投标书抬头）、"招标人""招标单位"处。
- 【厂家/品牌商=排除】产品制造商。出现在"厂家""制造商""生产企业""品牌""授权"等处（如开滋/KITZ、伯尔梅特等品牌对应的公司）。它只是货品来源，不是投标人。
- 【代理商=排除】除非它同时是盖章的投标单位。

规则：
- 公司全称含"有限公司/集团/实业/设备/科技/贸易/工程"等机构后缀。
- 【关键】只有当公司名旁有明确的"投标单位名称/投标人/（盖章）"标签时才返回；若本页是封面、投标书抬头、厂家资质/授权页（只有招标人或厂家名），一律返回空字符串。
只返回投标人公司全称（字符串），不确定就返回 ""。不要JSON，不要任何解释。"""


class DashScopeOCRProvider(LLMProvider):
    """Two-stage provider: OCR (table_parsing) → text LLM (JSON extraction).

    Supports multi-key rotation via DASHSCOPE_API_KEYS env var.  Each key gets
    its own semaphore (max _PER_KEY_CONCURRENCY concurrent calls) so that a
    single slow key doesn't block the others.  429 responses and transient
    connection errors are retried with linear backoff up to _MAX_RETRIES times.
    """

    name = "dashscope_ocr"

    DEFAULT_OCR_MODEL = "qwen-vl-ocr-latest"
    DEFAULT_LLM_MODEL = "qwen3.6-flash"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        ocr_model: str | None = None,
        llm_model: str | None = None,
    ):
        # Resolve key list: DASHSCOPE_API_KEYS (comma-separated) > api_key arg > single env key
        keys_env = os.getenv("DASHSCOPE_API_KEYS", "")
        keys = [k.strip() for k in keys_env.split(",") if k.strip()]
        if not keys:
            single = api_key or os.getenv("DASHSCOPE_API_KEY", "")
            if not single:
                raise ProviderError(
                    "DASHSCOPE_API_KEY not set; cannot initialise DashScopeOCRProvider"
                )
            keys = [single]

        self._keys = keys
        self._key_cycle = itertools.cycle(keys)
        self._key_lock = threading.Lock()
        self._per_key_sem: dict[str, threading.Semaphore] = {
            k: threading.Semaphore(_PER_KEY_CONCURRENCY) for k in keys
        }
        self._llm_clients: dict[str, OpenAI] = {}
        self._client_lock = threading.Lock()

        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_BASE_URL)
        self.ocr_model = ocr_model or os.getenv("DASHSCOPE_OCR_MODEL", self.DEFAULT_OCR_MODEL)
        self.llm_model = llm_model or os.getenv("DASHSCOPE_LLM_MODEL", self.DEFAULT_LLM_MODEL)
        self.model = f"{self.ocr_model}+{self.llm_model}"

        log.info(
            "DashScopeOCRProvider ready — %d key(s), concurrency=%d/key, ocr=%s, llm=%s",
            len(keys), _PER_KEY_CONCURRENCY, self.ocr_model, self.llm_model,
        )

    # ─── key / client helpers ─────────────────────────────────────────────

    def _next_key(self) -> str:
        with self._key_lock:
            return next(self._key_cycle)

    def _get_client(self, key: str) -> OpenAI:
        with self._client_lock:
            if key not in self._llm_clients:
                self._llm_clients[key] = OpenAI(api_key=key, base_url=self.base_url)
            return self._llm_clients[key]

    # ─── page classification (single OCR pass, HTML cached) ──────────────

    def ocr_pages_with_roles(
        self, images: list[bytes],
    ) -> tuple[list[tuple["PageClassification", str]], list[dict]]:
        """Stage 1 for all pages: OCR → HTML → classify role.

        Returns:
            (page_roles, failed_pages)
            - page_roles: list of (PageClassification, html) in page order
            - failed_pages: list of {"page": 1-based, "error": str} for failed OCR calls
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from apps.api.intelligence.page_classifier import classify_page, PageClassification, PageRole

        n = len(images)
        out: list[tuple[PageClassification, str] | None] = [None] * n
        failures: list[dict] = []
        workers = min(_PER_KEY_CONCURRENCY * len(self._keys), n)

        def _ocr_one(idx: int, image: bytes):
            html, _ = self._ocr_page(image)
            return idx, (classify_page(html), html), None

        with ThreadPoolExecutor(max_workers=workers) as exc:
            futs = {exc.submit(_ocr_one, i, img): i for i, img in enumerate(images)}
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    idx, result, _ = fut.result()
                    out[idx] = result
                except Exception as e:
                    log.warning("OCR page %d failed: %s", idx + 1, e)
                    out[idx] = (PageClassification(primary_role=PageRole.UNKNOWN), "")
                    failures.append({"page": idx + 1, "error": str(e)})

        return out, failures  # type: ignore[return-value]

    # ─── public API (called per-page by pipeline) ─────────────────────────

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
        page_html: str | None = None,
        table_grids=None,  # list[TableGrid] | None — structured input from table_parser
    ) -> ExtractionResponse:
        """Two-stage extraction for a single page image.

        If page_html is provided (pre-computed from ocr_pages_with_roles),
        Stage 1 OCR is skipped and the cached HTML is used directly.

        If table_grids is also provided (and doc_type is 'quote'), the Stage-2
        LLM receives structured TableGrid JSON instead of raw HTML, which reduces
        hallucination and enables row-level source_ref tracking.
        """
        if not images:
            raise ProviderError("extract() requires at least one image")

        t0 = time.time()
        total_tokens = 0

        if page_html is not None:
            html = page_html
            ocr_tokens = 0
        else:
            html, ocr_tokens = self._ocr_page(images[0])
            total_tokens += ocr_tokens

        if not html.strip():
            return ExtractionResponse(
                data={"items": []},
                raw_text="",
                tokens_used=total_tokens,
                provider=self.name,
                duration_ms=int((time.time() - t0) * 1000),
            )

        doc_type = self._guess_doc_type(prompt)

        if table_grids and doc_type == "quote":
            # Structured path: TableGrid JSON → LLM (lower token cost, row-level source_ref)
            from apps.api.intelligence.table_parser import grids_to_llm_json
            grid_json = grids_to_llm_json(table_grids)
            data, raw_text, llm_tokens = self._llm_call_json(
                _QUOTE_S2_TABLE_PROMPT, grid_json
            )
        else:
            data, raw_text, llm_tokens = self._llm_parse(html, doc_type)

        total_tokens += llm_tokens

        return ExtractionResponse(
            data=data,
            raw_text=raw_text,
            tokens_used=total_tokens,
            provider=f"{self.name}:{self.model}",
            duration_ms=int((time.time() - t0) * 1000),
        )

    def extract_doc_meta(self, meta_htmls: list[str]) -> dict:
        """Extract document-level metadata from cover/summary page HTMLs.

        Returns: {supplier_name, bid_total, bid_total_basis, tax_rate}
        """
        if not meta_htmls:
            return {
                "supplier_name": None,
                "bid_total": None,
                "bid_total_basis": "unknown",
                "tax_rate": None,
            }

        combined_html = "\n---\n".join(meta_htmls[:MAX_META_PAGES])
        t0 = time.time()
        key = self._next_key()
        client = self._get_client(key)
        sem = self._per_key_sem[key]
        sem.acquire()
        try:
            resp = client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": _META_S2_PROMPT},
                    {"role": "user", "content": combined_html},
                ],
                temperature=0.0,
                max_tokens=256,
                extra_body={"enable_thinking": False},
            )
        except Exception as e:
            sem.release()
            log.warning("Doc meta extraction error: %s", e)
            return {"supplier_name": None, "bid_total": None,
                    "bid_total_basis": "unknown", "tax_rate": None}
        sem.release()

        raw = (resp.choices[0].message.content or "").strip()
        if "</think>" in raw:
            raw = raw.split("</think>")[-1].strip()
        clean = raw
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        try:
            import json as _json
            data = _json.loads(clean)
        except Exception:
            log.warning("Doc meta JSON parse failed: %s", raw[:200])
            data = {}

        return {
            "supplier_name": data.get("supplier_name") or None,
            "bid_total": data.get("bid_total"),
            "bid_total_basis": data.get("bid_total_basis") or "unknown",
            "tax_rate": data.get("tax_rate"),
        }

    def extract_supplier_name_from_cover(
        self, cover_images: list[bytes], max_pages: int = 10,
    ) -> str:
        """Fallback: scan the front pages for the bidder (投标人) company name.

        Runs all candidate pages in parallel, returns the result from the
        earliest page that yields a confident bidder name.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from apps.api.intelligence.aggregator import _pick_supplier_name

        pages = cover_images[:max_pages]
        if not pages:
            return ""

        def _try_page(idx: int, page_bytes: bytes) -> tuple[int, str]:
            try:
                html, _ = self._ocr_page(page_bytes)
                if not html.strip():
                    return idx, ""
                key = self._next_key()
                client = self._get_client(key)
                sem = self._per_key_sem[key]
                sem.acquire()
                try:
                    resp = client.chat.completions.create(
                        model=self.llm_model,
                        messages=[
                            {"role": "system", "content": _SUPPLIER_NAME_PROMPT},
                            {"role": "user", "content": html},
                        ],
                        temperature=0.0,
                        max_tokens=100,
                        extra_body={"enable_thinking": False},
                    )
                finally:
                    sem.release()
                name = (resp.choices[0].message.content or "").strip()
                if "</think>" in name:
                    name = name.split("</think>")[-1].strip()
                name = name.strip('"').strip("'").strip()
                if name and name not in {"无", "找不到", "null", "None", ""}:
                    return idx, _pick_supplier_name([name], set()) or ""
            except Exception as e:
                log.warning("Supplier name cover fallback error (page %d): %s", idx + 1, e)
            return idx, ""

        workers = min(_PER_KEY_CONCURRENCY * len(self._keys), len(pages))
        found: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as exc:
            futs = {exc.submit(_try_page, i, img): i for i, img in enumerate(pages)}
            for fut in as_completed(futs):
                try:
                    idx, name = fut.result()
                    if name:
                        found[idx] = name
                except Exception:
                    pass

        # Return earliest-page confident result
        for i in range(len(pages)):
            if found.get(i):
                log.info("Supplier name recovered from cover page %d: %r", i + 1, found[i])
                return found[i]
        return ""

    # ─── Stage 1: OCR ─────────────────────────────────────────────────────

    def _ocr_page(self, page_bytes: bytes) -> tuple[str, int]:
        """Qwen-VL-OCR table_parsing on one page. Retries on 429 / connection errors."""
        b64 = base64.b64encode(page_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            sem = self._per_key_sem[key]
            sem.acquire()
            try:
                resp = dashscope.MultiModalConversation.call(
                    api_key=key,
                    model=self.ocr_model,
                    messages=[{
                        "role": "user",
                        "content": [{
                            "image": data_uri,
                            "min_pixels": 3136,
                            "max_pixels": 8388608,
                        }],
                    }],
                    ocr_options={"task": "table_parsing"},
                )
            except Exception as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_DELAY * (attempt + 1)
                    log.warning("OCR connection error (attempt %d/%d), retry in %ds: %s",
                                attempt + 1, _MAX_RETRIES, wait, e)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR call failed after {_MAX_RETRIES} retries: {e}") from e
            sem.release()

            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_DELAY * (attempt + 1)
                    log.warning("OCR 429 rate limited (attempt %d/%d), retry in %ds",
                                attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR 429 after {_MAX_RETRIES} retries")

            if resp.status_code != 200:
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_DELAY * (attempt + 1)
                    log.warning("OCR %d error (attempt %d/%d), retry in %ds: %s",
                                resp.status_code, attempt + 1, _MAX_RETRIES, wait, resp.message)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"OCR error {resp.status_code}: {resp.message}")

            text = ""
            if resp.output and resp.output.choices:
                choice = resp.output.choices[0]
                if choice.message and choice.message.content:
                    for part in choice.message.content:
                        if hasattr(part, "text"):
                            text += part.text
                        elif isinstance(part, dict) and "text" in part:
                            text += part["text"]

            tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
            return text, tokens

        return "", 0

    # ─── Stage 2: Text LLM ────────────────────────────────────────────────

    def _llm_parse(self, html: str, doc_type: str) -> tuple[dict, str, int]:
        """Parse OCR HTML into structured JSON via Stage-2 LLM."""
        s2_prompt = _QUOTE_S2_PROMPT if doc_type == "quote" else _TENDER_S2_PROMPT
        return self._llm_call_json(s2_prompt, html)

    def _llm_call_json(
        self,
        system_prompt: str,
        user_content: str,
        *,
        enable_thinking: bool = False,
    ) -> tuple[dict, str, int]:
        """Call the text LLM with retry; return (parsed_dict, raw_text, tokens)."""
        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            client = self._get_client(key)
            sem = self._per_key_sem[key]
            sem.acquire()
            try:
                resp = client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.0,  # 降低抽取非确定性(召回波动);见 design/05 §9.1
                    max_tokens=8192,
                    extra_body={"enable_thinking": enable_thinking},
                )
            except openai.RateLimitError as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_DELAY * (attempt + 1)
                    log.warning("LLM 429 rate limited (attempt %d/%d), retry in %ds",
                                attempt + 1, _MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"LLM 429 after {_MAX_RETRIES} retries") from e
            except Exception as e:
                sem.release()
                if attempt < _MAX_RETRIES - 1:
                    wait = _RETRY_DELAY * (attempt + 1)
                    log.warning("LLM connection error (attempt %d/%d), retry in %ds: %s",
                                attempt + 1, _MAX_RETRIES, wait, e)
                    time.sleep(wait)
                    continue
                raise ProviderError(f"LLM call failed after {_MAX_RETRIES} retries: {e}") from e
            sem.release()

            raw = (resp.choices[0].message.content or "").strip()
            tokens = resp.usage.total_tokens if resp.usage else 0

            clean = raw
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)
            if "</think>" in clean:
                clean = clean.split("</think>")[-1].strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*", "", clean)
                clean = re.sub(r"\s*```$", "", clean)

            try:
                data = json.loads(clean)
                return data, raw, tokens
            except (json.JSONDecodeError, ValueError) as e:
                if attempt < _MAX_RETRIES - 1:
                    log.warning("LLM JSON parse failed (attempt %d/%d): %s — raw: %s",
                                attempt + 1, _MAX_RETRIES, e, raw[:200])
                    continue
                raise ProviderError(
                    f"LLM JSON parse failed: {e}\nRaw: {raw[:300]}"
                ) from e

        raise ProviderError("LLM extraction failed after all retries")

    # ─── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _guess_doc_type(prompt: str) -> str:
        if "报价" in prompt or "quote" in prompt.lower():
            return "quote"
        return "tender"
