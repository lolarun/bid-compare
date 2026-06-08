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

返回JSON格式：
{"supplier_name": "供应商名称", "items": [{"material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率小数, "remark": "备注"}]}

如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""

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

    # ─── public API (called per-page by pipeline) ─────────────────────────

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
    ) -> ExtractionResponse:
        """Two-stage extraction for a single page image."""
        if not images:
            raise ProviderError("extract() requires at least one image")

        t0 = time.time()
        total_tokens = 0

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
        data, raw_text, llm_tokens = self._llm_parse(html, doc_type)
        total_tokens += llm_tokens

        return ExtractionResponse(
            data=data,
            raw_text=raw_text,
            tokens_used=total_tokens,
            provider=f"{self.name}:{self.model}",
            duration_ms=int((time.time() - t0) * 1000),
        )

    def extract_supplier_name_from_cover(
        self, cover_images: list[bytes], max_pages: int = 10,
    ) -> str:
        """Fallback: scan the front pages for the bidder (投标人) company name.

        Called by the pipeline when supplier_name is still empty after aggregation.
        The bidder name is sometimes buried deep (e.g. on the stamped 投标单位名称
        page, not the cover — the cover often shows only the 招标人/buyer). So we
        scan up to *max_pages* front pages and early-stop at the first confident
        bidder name. The prompt is instructed to exclude the 招标人/buyer.

        Returns a non-empty company name or "" if nothing found.
        """
        from apps.api.intelligence.aggregator import _pick_supplier_name  # avoid circular at module level

        for page_bytes in cover_images[:max_pages]:
            html, _ = self._ocr_page(page_bytes)
            if not html.strip():
                continue

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
            except Exception as e:
                sem.release()
                log.warning("Supplier name cover fallback error: %s", e)
                continue
            sem.release()

            name = (resp.choices[0].message.content or "").strip()
            if "</think>" in name:
                name = name.split("</think>")[-1].strip()
            name = name.strip('"').strip("'").strip()

            if name and name not in {"无", "找不到", "null", "None", ""}:
                result = _pick_supplier_name([name], set())
                if result:
                    log.info("Supplier name recovered from cover: %r", result)
                    return result

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
        """Parse OCR HTML into structured JSON. Retries on 429 / JSON parse failure."""
        s2_prompt = _QUOTE_S2_PROMPT if doc_type == "quote" else _TENDER_S2_PROMPT

        for attempt in range(_MAX_RETRIES):
            key = self._next_key()
            client = self._get_client(key)
            sem = self._per_key_sem[key]
            sem.acquire()
            try:
                resp = client.chat.completions.create(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": s2_prompt},
                        {"role": "user", "content": html},
                    ],
                    temperature=0.0,  # 降低抽取非确定性(召回波动);见 design/05 §9.1
                    max_tokens=8192,
                    extra_body={"enable_thinking": False},
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
