"""MockProvider — deterministic stub for tests and fallback when API key absent.

Three modes:
1. Fixture mode: read JSON fixtures from `mock_responses/{schema_hash}.json`
2. Inline mode: caller injects `MockProvider(canned_response=...)`
3. Default mode: returns a minimal valid object derived from the schema's required fields

This lets the rest of the system (ingestion service, routes, frontend) work
end-to-end without consuming LLM quota.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from apps.api.intelligence.base import LLMProvider, ExtractionResponse


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(
        self,
        canned: dict[str, Any] | None = None,
        fixture_dir: str | Path | None = None,
    ):
        self.canned = canned
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
    ) -> ExtractionResponse:
        t0 = time.time()
        if self.canned is not None:
            data = self.canned
        elif self.fixture_dir:
            data = self._load_fixture(schema)
        else:
            data = self._minimal_from_schema(schema)
        return ExtractionResponse(
            data=data,
            raw_text=json.dumps(data, ensure_ascii=False),
            confidence=1.0,
            tokens_used=0,
            provider=self.name,
            duration_ms=int((time.time() - t0) * 1000),
            metadata={"image_count": len(images)},
        )

    # ─── helpers ───────────────────────────────────────────────────────────
    def _load_fixture(self, schema: dict[str, Any]) -> dict[str, Any]:
        # Determine fixture by checking which top-level required key is present
        required = schema.get("required") or []
        kind = "tender" if "project_name" in required else "quote"
        fixture_path = self.fixture_dir / f"{kind}.json" if self.fixture_dir else None
        if fixture_path and fixture_path.is_file():
            return json.loads(fixture_path.read_text(encoding="utf-8"))
        return self._minimal_from_schema(schema)

    @staticmethod
    def _minimal_from_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """Return the simplest object that satisfies `required` top-level fields."""
        result: dict[str, Any] = {}
        for field in schema.get("required", []):
            spec = (schema.get("properties") or {}).get(field, {})
            t = spec.get("type")
            if t == "string":
                result[field] = ""
            elif t == "array":
                result[field] = []
            elif t == "object":
                result[field] = {}
            elif t in ("number", "integer"):
                result[field] = 0
            else:
                result[field] = None
        # Sensible defaults for the two schemas we care about
        if "items" in result and isinstance(result["items"], list):
            result.setdefault("items", [])
        return result
