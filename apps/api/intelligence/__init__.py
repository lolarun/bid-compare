"""Intelligence engine layer — pluggable LLM/OCR providers for document extraction.

Architecture:
- base.LLMProvider: abstract base class for extraction providers
- providers.DashScopeOCRProvider: two-stage OCR (Qwen-VL-OCR) + LLM (qwen3.6-flash)
- providers.MockProvider: deterministic stub for tests / fallback when no API key
- pipeline.ExtractionPipeline: orchestrates loader -> provider -> postprocess
- schemas.TENDER_SCHEMA / QUOTE_SCHEMA: JSON Schema targets for each document type
- prompts.TENDER_PROMPT / QUOTE_PROMPT: business-tuned prompt templates
"""

from apps.api.intelligence.base import (
    LLMProvider,
    ExtractionResponse,
    ProviderError,
)
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider

__all__ = [
    "LLMProvider",
    "ExtractionResponse",
    "ProviderError",
    "TENDER_SCHEMA",
    "QUOTE_SCHEMA",
    "TENDER_PROMPT",
    "QUOTE_PROMPT",
    "ExtractionPipeline",
    "MockProvider",
    "DashScopeOCRProvider",
]
