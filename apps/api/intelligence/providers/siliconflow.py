"""SiliconFlowProvider — vision models via SiliconFlow OpenAI-compatible API.

SiliconFlow (硅基流动) hosts open-source vision models including DeepSeek-VL2,
Qwen2-VL, InternVL etc., all accessible via an OpenAI-compatible endpoint.

Config (apps/api/.env):
  SILICONFLOW_API_KEY   — required
  SILICONFLOW_BASE_URL  — default https://api.siliconflow.cn/v1
  SILICONFLOW_MODEL     — default deepseek-ai/deepseek-vl2

No content-moderation blocking has been observed on SiliconFlow for standard
commercial document images (seals, tables, etc.).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

from openai import OpenAI, APIError, BadRequestError

from apps.api.intelligence.base import (
    LLMProvider, ExtractionResponse, ProviderError, ContentModerationError,
)
# Reuse JSON helpers from qwen_vl — identical format
from apps.api.intelligence.providers.qwen_vl import QwenVLProvider as _QwenHelpers

log = logging.getLogger(__name__)


class SiliconFlowProvider(LLMProvider):
    name = "siliconflow"
    DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
    DEFAULT_MODEL = "deepseek-ai/deepseek-vl2"
    BASE_TIMEOUT_S = 30
    PER_PAGE_TIMEOUT_S = 25

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        key = api_key or os.getenv("SILICONFLOW_API_KEY")
        if not key:
            raise ProviderError(
                "SILICONFLOW_API_KEY not set; cannot initialise SiliconFlowProvider"
            )
        self.client = OpenAI(
            api_key=key,
            base_url=base_url or os.getenv("SILICONFLOW_BASE_URL", self.DEFAULT_BASE_URL),
        )
        self.model = (
            model
            or os.getenv("SILICONFLOW_MODEL")
            or self.DEFAULT_MODEL
        )
        log.info("SiliconFlowProvider ready — model=%s", self.model)

    # ─── public API ───────────────────────────────────────────────────────
    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int | None = None,
    ) -> ExtractionResponse:
        if not images:
            raise ProviderError("extract() requires at least one image")

        effective_timeout = (
            timeout if timeout is not None
            else self.BASE_TIMEOUT_S + self.PER_PAGE_TIMEOUT_S * len(images)
        )
        full_prompt = _QwenHelpers._build_prompt(prompt, schema)

        content: list[dict[str, Any]] = []
        for img in images:
            b64 = base64.b64encode(img).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        content.append({"type": "text", "text": full_prompt})

        t0 = time.time()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                timeout=effective_timeout,
            )
        except BadRequestError as e:
            err_str = str(e)
            if "content" in err_str.lower() and (
                "inappropriate" in err_str.lower()
                or "inspection" in err_str.lower()
                or "moderation" in err_str.lower()
            ):
                raise ContentModerationError(
                    f"Content moderation blocked (model={self.model}): {e}"
                ) from e
            raise ProviderError(f"SiliconFlow API bad request: {e}") from e
        except APIError as e:
            raise ProviderError(f"SiliconFlow API error: {e}") from e

        raw = (resp.choices[0].message.content or "").strip()
        try:
            data = _QwenHelpers._parse_json_strict(raw)
        except (ValueError, json.JSONDecodeError) as e:
            raise ProviderError(
                f"SiliconFlow JSON parse failed: {e}\nRaw: {raw[:300]}"
            ) from e

        return ExtractionResponse(
            data=data,
            raw_text=raw,
            confidence=1.0,
            tokens_used=getattr(resp.usage, "total_tokens", 0) if resp.usage else 0,
            provider=f"{self.name}:{self.model}",
            duration_ms=int((time.time() - t0) * 1000),
        )
