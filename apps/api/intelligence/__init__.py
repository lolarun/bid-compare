"""Intelligence engine layer — pluggable LLM/OCR providers for document extraction.

Architecture:
- base.LLMProvider: abstract base class for extraction providers
- providers.DashScopeOCRProvider: two-stage OCR (Qwen-VL-OCR) + LLM (qwen3.6-flash)
- providers.MockProvider: deterministic stub for tests / fallback when no API key
- pipeline.ExtractionPipeline: orchestrates loader -> provider -> postprocess (VL-direct only)
- schemas.TENDER_SCHEMA / QUOTE_SCHEMA: JSON Schema targets for each document type

prompts.py (TENDER_PROMPT/QUOTE_PROMPT/META_EXTRACTION_PROMPT/TENDER_BIDLIST_PROMPT/
TENDER_BRANDTABLE_PROMPT) was deleted 2026-08-11 with the legacy per-page OCR→HTML
chain it served — see vl_quote.py / vl_tender.py for the live VL-direct prompts.
"""

from apps.api.intelligence.base import (
    ExtractionResponse,
    LLMProvider,
    ProviderError,
)
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
from apps.api.intelligence.providers.mock import MockProvider
from apps.api.intelligence.schemas import QUOTE_SCHEMA, TENDER_SCHEMA

__all__ = [
    "LLMProvider",
    "ExtractionResponse",
    "ProviderError",
    "TENDER_SCHEMA",
    "QUOTE_SCHEMA",
    "ExtractionPipeline",
    "MockProvider",
    "DashScopeOCRProvider",
]
