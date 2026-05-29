"""DashScopeOCRProvider — two-stage OCR+LLM extraction via Alibaba DashScope.

Stage 1: Qwen-VL-OCR (table_parsing) → HTML tables per page
Stage 2: qwen3.6-flash → structured JSON per page

This provider replaces the single-stage VL approach with better accuracy,
no content-moderation blocking, and lower cost.

Config (apps/api/.env):
  DASHSCOPE_API_KEY   — required
  DASHSCOPE_BASE_URL  — default https://dashscope.aliyuncs.com/compatible-mode/v1
  DASHSCOPE_OCR_MODEL — default qwen-vl-ocr-latest
  DASHSCOPE_LLM_MODEL — default qwen3.6-flash
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any

import dashscope
from openai import OpenAI

from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError,
)

log = logging.getLogger(__name__)

# ── Stage 2 prompts (OCR HTML → structured JSON) ────────────────────────
# These are specialised for HTML table input, distinct from the VL prompts
# in prompts.py which operate on raw images.

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
- 只提取材料报价行，不要表头、合计行、小计行
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）
- 总价若已标注使用原值，否则留null
- 税率用小数如0.13表示13%
- 品牌按原文
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "供应商名称", "items": [{"material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率小数, "remark": "备注"}]}

如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""


class DashScopeOCRProvider(LLMProvider):
    """Two-stage provider: OCR (table_parsing) → text LLM (JSON extraction)."""

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
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ProviderError(
                "DASHSCOPE_API_KEY not set; cannot initialise DashScopeOCRProvider"
            )
        self.api_key = key
        self.base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_BASE_URL)
        self.ocr_model = ocr_model or os.getenv("DASHSCOPE_OCR_MODEL", self.DEFAULT_OCR_MODEL)
        self.llm_model = llm_model or os.getenv("DASHSCOPE_LLM_MODEL", self.DEFAULT_LLM_MODEL)

        # Stage 1 uses dashscope SDK directly
        dashscope.api_key = key

        # Stage 2 uses OpenAI-compatible client
        self._llm_client = OpenAI(api_key=key, base_url=self.base_url)

        # Expose model for logging (pipeline._log_extraction reads this)
        self.model = f"{self.ocr_model}+{self.llm_model}"

        log.info(
            "DashScopeOCRProvider ready — ocr=%s, llm=%s",
            self.ocr_model, self.llm_model,
        )

    # ─── public API (called per-page by pipeline) ────────────────────────

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
    ) -> ExtractionResponse:
        """Two-stage extraction for a single page image.

        Called by ExtractionPipeline._extract_page() once per page.
        Stage 1: OCR the image → HTML
        Stage 2: LLM parses HTML → structured JSON
        """
        if not images:
            raise ProviderError("extract() requires at least one image")

        t0 = time.time()
        total_tokens = 0

        # Stage 1: OCR → HTML
        html, ocr_tokens = self._ocr_page(images[0])
        total_tokens += ocr_tokens

        if not html.strip():
            # Empty page (blank or non-table content)
            return ExtractionResponse(
                data={"items": []},
                raw_text="",
                tokens_used=total_tokens,
                provider=self.name,
                duration_ms=int((time.time() - t0) * 1000),
            )

        # Stage 2: LLM → JSON
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

    # ─── Stage 1: OCR ────────────────────────────────────────────────────

    def _ocr_page(self, page_bytes: bytes) -> tuple[str, int]:
        """Run Qwen-VL-OCR table_parsing on a single page. Returns (html, tokens)."""
        b64 = base64.b64encode(page_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        try:
            resp = dashscope.MultiModalConversation.call(
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
            raise ProviderError(f"OCR call failed: {e}") from e

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

        tokens = 0
        if resp.usage:
            tokens = getattr(resp.usage, "total_tokens", 0) or 0

        return text, tokens

    # ─── Stage 2: Text LLM ──────────────────────────────────────────────

    def _llm_parse(self, html: str, doc_type: str) -> tuple[dict, str, int]:
        """Parse OCR HTML into structured JSON. Returns (data, raw_text, tokens)."""
        s2_prompt = _QUOTE_S2_PROMPT if doc_type == "quote" else _TENDER_S2_PROMPT

        try:
            resp = self._llm_client.chat.completions.create(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": s2_prompt},
                    {"role": "user", "content": html},
                ],
                temperature=0.1,
                max_tokens=8192,
                extra_body={"enable_thinking": False},
            )
        except Exception as e:
            raise ProviderError(f"LLM call failed: {e}") from e

        raw = (resp.choices[0].message.content or "").strip()
        tokens = resp.usage.total_tokens if resp.usage else 0

        # Clean markdown fences and thinking tags
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
        except (json.JSONDecodeError, ValueError) as e:
            raise ProviderError(
                f"LLM JSON parse failed: {e}\nRaw: {raw[:300]}"
            ) from e

        return data, raw, tokens

    # ─── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _guess_doc_type(prompt: str) -> str:
        """Infer tender/quote from the extraction prompt text."""
        if "报价" in prompt or "quote" in prompt.lower():
            return "quote"
        return "tender"
