"""QwenVLProvider — Qwen-VL via DashScope OpenAI-compatible endpoint.

Uses the official OpenAI Python SDK (>=1.30) in compatible mode against
`https://dashscope.aliyuncs.com/compatible-mode/v1`.

Model resolution order at startup (first reachable wins):
1. env LLM_VISION_MODEL (default qwen3-vl-plus)
2. env LLM_VISION_MODEL_FALLBACK (default qwen-vl-max)
3. baked-in DEFAULT_MODELS list

The provider performs one retry on transient errors and falls back to the
next candidate model if the preferred one returns a 4xx model-not-found.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

from openai import OpenAI, APIError, BadRequestError

from apps.api.intelligence.base import LLMProvider, ExtractionResponse, ProviderError

log = logging.getLogger(__name__)


class QwenVLProvider(LLMProvider):
    name = "qwen_vl"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODELS = ["qwen3-vl-plus", "qwen-vl-max", "qwen-vl-plus"]

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        models: list[str] | None = None,
    ):
        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise ProviderError(
                "DASHSCOPE_API_KEY not set; cannot initialise QwenVLProvider"
            )
        self.client = OpenAI(
            api_key=key,
            base_url=base_url or os.getenv("DASHSCOPE_BASE_URL", self.DEFAULT_BASE_URL),
        )
        candidates = self._build_candidates(model, models)
        self.candidates = candidates
        # Don't probe at init time; on first call we'll try the head and fall
        # back per-call if needed. Probing on init blocks app startup.
        self.model = candidates[0]

    @staticmethod
    def _build_candidates(model: str | None, models: list[str] | None) -> list[str]:
        explicit: list[str] = []
        if model:
            explicit.append(model)
        if models:
            explicit.extend(models)
        env_preferred = os.getenv("LLM_VISION_MODEL")
        env_fallback = os.getenv("LLM_VISION_MODEL_FALLBACK")
        for m in (env_preferred, env_fallback):
            if m and m not in explicit:
                explicit.append(m)
        for m in QwenVLProvider.DEFAULT_MODELS:
            if m not in explicit:
                explicit.append(m)
        return explicit

    # ─── public API ───────────────────────────────────────────────────────
    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
    ) -> ExtractionResponse:
        if not images:
            raise ProviderError("extract() requires at least one image")
        full_prompt = self._build_prompt(prompt, schema)
        content: list[dict[str, Any]] = []
        for img in images:
            b64 = base64.b64encode(img).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        content.append({"type": "text", "text": full_prompt})

        last_err: Exception | None = None
        for candidate in self.candidates:
            t0 = time.time()
            try:
                resp = self.client.chat.completions.create(
                    model=candidate,
                    messages=[{"role": "user", "content": content}],
                    timeout=timeout,
                )
                raw = (resp.choices[0].message.content or "").strip()
                data = self._parse_json_strict(raw)
                self.model = candidate  # remember successful model
                return ExtractionResponse(
                    data=data,
                    raw_text=raw,
                    confidence=1.0,
                    tokens_used=getattr(resp.usage, "total_tokens", 0)
                    if resp.usage
                    else 0,
                    provider=f"{self.name}:{candidate}",
                    duration_ms=int((time.time() - t0) * 1000),
                )
            except BadRequestError as e:
                # Likely "model not found / not authorized"; try next candidate
                log.warning("QwenVL model %s rejected: %s", candidate, e)
                last_err = e
                continue
            except APIError as e:
                # Transient — log and try next
                log.warning("QwenVL APIError on %s: %s", candidate, e)
                last_err = e
                continue
            except (ValueError, json.JSONDecodeError) as e:
                log.error("QwenVL JSON parse failed on %s: %s", candidate, e)
                last_err = e
                continue
        raise ProviderError(
            f"All candidate models failed: {self.candidates}. Last error: {last_err}"
        )

    # ─── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _build_prompt(business_prompt: str, schema: dict[str, Any]) -> str:
        return (
            business_prompt.rstrip()
            + "\n\n严格按以下 JSON Schema 输出。仅返回 JSON，不要任何解释或 markdown 围栏：\n"
            + json.dumps(schema, ensure_ascii=False)
        )

    @staticmethod
    def _parse_json_strict(text: str) -> dict[str, Any]:
        """Tolerant JSON parser: strips ```json fences, extracts {...} substring."""
        if not text:
            raise ValueError("empty LLM response")
        s = text.strip()
        # Strip markdown code fences
        if s.startswith("```"):
            # remove first line (``` or ```json) and last fence
            lines = s.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            s = "\n".join(lines).strip()
        # Extract first {…} block
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            s = s[start : end + 1]
        data = json.loads(s)
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object, got {type(data).__name__}")
        return data
