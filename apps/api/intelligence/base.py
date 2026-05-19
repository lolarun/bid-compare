"""Intelligence engine abstract base — LLMProvider contract + response types.

Providers MUST implement `extract(images, schema, prompt) -> ExtractionResponse`.
The response carries both the parsed dict and the raw text for debugging.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """Raised when a provider fails to produce a usable extraction."""


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
    """Vision-capable LLM provider interface.

    Implementations:
    - QwenVLProvider: Qwen-VL via DashScope (OpenAI-compatible mode)
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
