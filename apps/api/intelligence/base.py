"""Intelligence engine abstract base — LLMProvider contract + response types.

Providers MUST implement `extract(...)` and `vl_extract_csv(...)`. The response
carries both the parsed dict and the raw text for debugging.

评审 N3：这份 ABC 此前只声明了 extract()，而生产链真正依赖的契约是
vl_extract_csv——pipeline.py/tender_pdf.py 用 hasattr(provider,
"vl_extract_csv") 判断走不走 VL-direct，是"未声明方法的 hasattr 私有嗅探"，
接口名字实不符（HANDOFF 记录的"provider 缺一个方法就静默换路"教训根源就在
这——能力靠 hasattr 而不是靠声明，缺了就无声降级）。批次2已经把"静默降级"
改成了"显式报错"，这里再补上声明本身：两个 shipped provider（DashScopeOCR
Provider、MockProvider）此前就已经无条件实现了 vl_extract_csv，提升为
@abstractmethod 不改变任何行为，只是让 ABC 承认现状——新写一个 LLMProvider
子类若没实现它，会在类定义/实例化时就报错，而不是等到某次调用才发现。
pipeline.py/tender_pdf.py 里的 hasattr 判断保留：ABC 保证了"合法子类必有此
方法"，但判断本身现在读作"防御性守卫"而非"能力探测"，说明见各调用点注释。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """Raised when a provider fails to produce a usable extraction."""


class ContentModerationError(ProviderError):
    """Raised when the upstream API rejects image content (data_inspection_failed).

    This is a content issue, NOT a model availability issue — the model should
    NOT be blacklisted; the batch should be skipped by the pipeline.
    """


@dataclass
class ExtractionResponse:
    """Provider output. `data` is the parsed JSON dict matching the requested schema."""

    data: dict[str, Any]
    raw_text: str = ""
    confidence: float = 1.0
    tokens_used: int = 0
    provider: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """LLM/OCR provider interface for document extraction.

    Implementations:
    - DashScopeOCRProvider: two-stage OCR (Qwen-VL-OCR) + LLM (qwen3.6-flash)
    - MockProvider: deterministic stub for tests
    """

    name: str = "abstract"

    @abstractmethod
    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
    ) -> ExtractionResponse:
        """Extract structured data from image(s) according to JSON schema.

        Args:
            images: PNG/JPG bytes (PDFs already converted by the loader).
            schema: JSON Schema describing the desired output shape.
            prompt: Business-tuned instruction text.
            timeout: Network/inference timeout in seconds.

        Returns:
            ExtractionResponse with `data` populated from the LLM's JSON output.

        Raises:
            ProviderError: If extraction fails irrecoverably.
        """
        raise NotImplementedError

    @abstractmethod
    def vl_extract_csv(
        self,
        images: list[bytes],
        prompt: str,
        *,
        model: str | None = None,
        labels: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        """Whole-document VL-direct extraction: page images → CSV text.

        This is the actual production contract (VL-direct is the only
        recognition path for both quote and tender documents — see
        vl_quote.py / vl_tender.py / docs/design/21). `labels` distinguishes
        the orientation-probe call (rotation candidates per page) from the
        main extraction/meta calls — see DashScopeOCRProvider.vl_extract_csv
        for the convention.

        Args:
            images: PNG/JPG bytes for every page (or probe crops) in one call.
            prompt: Business-tuned instruction text (extraction/meta/orient).
            model: Provider-specific model id override.
            labels: Present only for orientation-probe calls.

        Returns:
            Raw CSV (or `key: value` lines for meta calls) text from the model.

        Raises:
            ProviderError: If the call fails irrecoverably.
        """
        raise NotImplementedError
