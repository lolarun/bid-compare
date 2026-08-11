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
import re
from pathlib import Path
from typing import Any

from apps.api.intelligence.base import LLMProvider, ExtractionResponse


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(
        self,
        canned: dict[str, Any] | None = None,
        fixture_dir: str | Path | None = None,
        vl_csv: str | None = None,
    ):
        self.canned = canned
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.vl_csv = vl_csv

    # ─── VL-direct 能力 ──────────────────────────────────────────────────────
    #
    # 没有这个方法时，`ExtractionPipeline.extract_quote` 的 VL 分支条件
    # `hasattr(provider, "vl_extract_csv")` 恒为 False —— **所有基于 mock 的集成
    # 测试都静默落回 legacy**，哪怕 QUOTE_RECOGNIZER=vl_direct。也就是说在此之前
    # VL 分支在测试里根本走不到，而且没有任何提示。

    def vl_extract_csv(
        self, images: list[bytes], prompt: str, *,
        model: str | None = None, labels: list[str] | None = None, **_kw,
    ) -> str:
        """抽取与方向预检共用此方法，靠 `labels` 区分（与真实 provider 一致）。"""
        if labels:
            # 方向预检：从 PAGE_<n>_ROT_<deg> 标签里取出页号，一律答"不用转"。
            pages = sorted({int(m.group(1)) for l in labels
                            if (m := re.match(r"PAGE_(\d+)_ROT_", l))})
            return "\n".join(f"{p},0" for p in pages)
        if self.vl_csv is not None:
            return self.vl_csv
        if self.fixture_dir and (self.fixture_dir / "quote_vl.csv").exists():
            return (self.fixture_dir / "quote_vl.csv").read_text(encoding="utf-8")
        # 兜底：每页一行的最小合法 CSV。行数随页数变化，便于断言"页→行"的归属。
        head = "row_type,名称,规格,单位,数量,单价,合价,copy_no,page"
        body = [f"detail,模拟材料{i},SPEC-{i},米,{i + 1},10.00,{(i + 1) * 10:.2f},1,{i + 1}"
                for i in range(len(images))]
        return "\n".join([head, *body])

    def ocr_pages_with_roles(
        self, images: list[bytes],
    ) -> tuple[list[tuple[Any, str]], list]:
        """Stub: every page classified as QUOTE_TABLE with empty HTML."""
        from apps.api.intelligence.page_classifier import (
            PageRole, PageClassification,
        )
        stub_html = "<table><tr><td>mock</td></tr></table>"
        cls = PageClassification(primary_role=PageRole.QUOTE_TABLE)
        return [(cls, stub_html) for _ in images], []

    def extract_doc_meta(self, meta_htmls: list[str]) -> dict:
        return {"supplier_name": None, "bid_total": None,
                "bid_total_basis": "unknown", "tax_rate": None}

    def classify_pages_visual(self, thumbnails: list[bytes], doc_type: str, **_kw):
        """Stub: every page = table_header（保留旧 mock 行为：所有页都是目标页）。"""
        role = "tender_table_header" if doc_type == "tender" else "quote_table_header"
        out = [{"page": i + 1, "role": role, "confidence": 1.0,
                "contains_table": True, "orientation": 0,
                "continues_from_page": None, "mixed_content": False,
                "evidence": ["mock"], "source": "flash"}
               for i in range(len(thumbnails))]
        return out, []

    def review_pages_visual(self, page_image, neighbor_thumbs, flash_result, page_no, **_kw):
        return dict(flash_result)

    def extract(
        self,
        images: list[bytes],
        schema: dict[str, Any],
        prompt: str,
        timeout: int = 90,
        page_html: str | None = None,
        table_grids=None,  # accepted but ignored — mock always uses canned data
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
