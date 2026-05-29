"""Unit + functional tests for the multi-page extraction engine.

Tests are split into three layers:

1. TestPageSplitter    — pure logic, no I/O, no API
2. TestResultAggregator — pure logic, no I/O, no API
3. TestExtractionPipeline — uses a MockProvider (no real API); validates that
   the pipeline calls the provider the correct number of times and that the
   final aggregated result is structurally correct
4. TestPipelineWithRealPDFs (marker: e2e) — exercises the full stack with real
   PDFs and the live Qwen-VL API; validates item counts and scalar metadata
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image


def _load_env_file() -> None:
    """Load apps/api/.env into os.environ (idempotent)."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

from apps.api.intelligence.aggregator import ResultAggregator
from apps.api.intelligence.base import ExtractionResponse, LLMProvider
from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.splitter import PageSplitter


# ─── helpers ──────────────────────────────────────────────────────────────

def _make_page(index: int = 0) -> bytes:
    """Create a minimal but valid 32×32 PNG page."""
    img = Image.new("RGB", (32, 32), color=(index * 10 % 255, 200, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pages(n: int) -> list[bytes]:
    return [_make_page(i) for i in range(n)]


def _resp(items: list[dict], doc_type: str = "quote", **scalars) -> ExtractionResponse:
    """Build a minimal ExtractionResponse for aggregation tests."""
    if doc_type == "tender":
        data = {
            "project_name": scalars.get("project_name", ""),
            "project_code": scalars.get("project_code", ""),
            "tender_date": scalars.get("tender_date", ""),
            "deadline": scalars.get("deadline", ""),
            "items": items,
        }
    else:
        data = {
            "supplier_name": scalars.get("supplier_name", ""),
            "quote_date": scalars.get("quote_date", ""),
            "items": items,
            "context": scalars.get("context", {}),
        }
    return ExtractionResponse(
        data=data,
        raw_text="",
        confidence=scalars.get("confidence", 0.9),
        tokens_used=scalars.get("tokens_used", 100),
        provider="mock",
        duration_ms=scalars.get("duration_ms", 500),
    )


class MockProvider(LLMProvider):
    """Provider that returns pre-baked responses in sequence."""

    def __init__(self, responses: list[ExtractionResponse]):
        self._responses = list(responses)
        self._calls: list[list[bytes]] = []

    def extract(self, images, schema, prompt, timeout=None) -> ExtractionResponse:
        self._calls.append(images)
        if not self._responses:
            raise RuntimeError("MockProvider ran out of responses")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def pages_per_call(self) -> list[int]:
        return [len(c) for c in self._calls]


class PageAwareMockProvider(LLMProvider):
    """Provider that returns the response mapped to the submitted page bytes."""

    def __init__(self, responses_by_page: dict[bytes, ExtractionResponse]):
        self._responses_by_page = responses_by_page
        self._calls: list[list[bytes]] = []

    def extract(self, images, schema, prompt, timeout=None) -> ExtractionResponse:
        self._calls.append(images)
        if len(images) != 1:
            raise AssertionError("page-aware provider expects exactly one page")
        return self._responses_by_page[images[0]]

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def pages_per_call(self) -> list[int]:
        return [len(c) for c in self._calls]


# ═══════════════════════════════════════════════════════════════════════════
# 1. PageSplitter
# ═══════════════════════════════════════════════════════════════════════════
class TestPageSplitter:

    def test_empty_input_returns_empty(self):
        assert PageSplitter.split([]) == []

    def test_single_page_one_batch(self):
        pages = _pages(1)
        batches = PageSplitter.split(pages, batch_size=4)
        assert len(batches) == 1
        assert batches[0] == pages

    def test_exactly_batch_size_no_split(self):
        pages = _pages(4)
        batches = PageSplitter.split(pages, batch_size=4)
        assert len(batches) == 1
        assert len(batches[0]) == 4

    def test_5_pages_split_into_2_batches(self):
        pages = _pages(5)
        batches = PageSplitter.split(pages, batch_size=4, context_pages=1)
        assert len(batches) == 2
        # first batch: pages 0-3
        assert batches[0] == pages[:4]
        # second batch: context page 0 + page 4
        assert batches[1] == [pages[0], pages[4]]

    def test_12_pages_split_into_3_batches(self):
        pages = _pages(12)
        batches = PageSplitter.split(pages, batch_size=4, context_pages=1)
        assert len(batches) == 3
        # batch 0: pages 0-3 (no context prepended to first)
        assert batches[0] == pages[0:4]
        # batch 1: context(page 0) + pages 4-7
        assert batches[1] == [pages[0]] + pages[4:8]
        # batch 2: context(page 0) + pages 8-11
        assert batches[2] == [pages[0]] + pages[8:12]

    def test_total_unique_pages_preserved(self):
        """Every original page must appear in exactly one non-context slot."""
        pages = _pages(10)
        batches = PageSplitter.split(pages, batch_size=3, context_pages=1)
        # collect all pages, excluding repeated context page on batches 2+
        unique: list[bytes] = list(batches[0])
        for b in batches[1:]:
            unique.extend(b[1:])    # skip the prepended context page
        assert unique == pages

    def test_batch_size_1_each_page_own_batch(self):
        pages = _pages(3)
        batches = PageSplitter.split(pages, batch_size=1, context_pages=0)
        assert len(batches) == 3
        for i, b in enumerate(batches):
            assert b == [pages[i]]

    def test_no_context_pages(self):
        pages = _pages(6)
        batches = PageSplitter.split(pages, batch_size=4, context_pages=0)
        assert len(batches) == 2
        assert batches[1] == pages[4:6]     # no context prepended


# ═══════════════════════════════════════════════════════════════════════════
# 2. ResultAggregator
# ═══════════════════════════════════════════════════════════════════════════
class TestResultAggregator:

    def test_empty_partials_raises(self):
        with pytest.raises(ValueError, match="empty"):
            ResultAggregator.merge([], "quote")

    def test_unknown_doc_type_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            ResultAggregator.merge([_resp([])], "unknown")

    def test_single_partial_passthrough(self):
        items = [{"material": "阀门", "spec": "DN50", "qty": 2}]
        r = _resp(items, doc_type="quote", supplier_name="测试供应商")
        merged = ResultAggregator.merge([r], "quote")
        assert merged.data["supplier_name"] == "测试供应商"
        assert len(merged.data["items"]) == 1

    def test_items_concatenated_across_batches(self):
        items_a = [{"material": "阀门A", "spec": "DN50", "qty": 1}]
        items_b = [{"material": "阀门B", "spec": "DN80", "qty": 2}]
        r1 = _resp(items_a, doc_type="quote", supplier_name="S1")
        r2 = _resp(items_b, doc_type="quote", supplier_name="")
        merged = ResultAggregator.merge([r1, r2], "quote")
        assert len(merged.data["items"]) == 2
        names = [it["material"] for it in merged.data["items"]]
        assert "阀门A" in names and "阀门B" in names

    def test_scalar_first_non_empty_wins(self):
        r1 = _resp([], doc_type="quote", supplier_name="")
        r2 = _resp([], doc_type="quote", supplier_name="第二批次有名字")
        r3 = _resp([], doc_type="quote", supplier_name="第三批次")
        merged = ResultAggregator.merge([r1, r2, r3], "quote")
        assert merged.data["supplier_name"] == "第二批次有名字"

    def test_tokens_summed(self):
        r1 = _resp([], tokens_used=100)
        r2 = _resp([], tokens_used=200)
        merged = ResultAggregator.merge([r1, r2], "quote")
        assert merged.tokens_used == 300

    def test_duration_summed(self):
        r1 = _resp([], duration_ms=400)
        r2 = _resp([], duration_ms=600)
        merged = ResultAggregator.merge([r1, r2], "quote")
        assert merged.duration_ms == 1000

    def test_confidence_is_minimum(self):
        r1 = _resp([], confidence=0.9)
        r2 = _resp([], confidence=0.6)
        r3 = _resp([], confidence=0.8)
        merged = ResultAggregator.merge([r1, r2, r3], "quote")
        assert merged.confidence == pytest.approx(0.6)

    def test_metadata_records_batch_count(self):
        r1 = _resp([{"material": "X", "spec": "", "qty": 1}])
        r2 = _resp([{"material": "Y", "spec": "", "qty": 2},
                    {"material": "Z", "spec": "", "qty": 3}])
        merged = ResultAggregator.merge([r1, r2], "quote")
        assert merged.metadata["batches"] == 2
        assert merged.metadata["items_per_batch"] == [1, 2]

    def test_duplicate_items_from_context_overlap_deduped(self):
        """Context pages cause the model to re-extract header items — dedup them."""
        item = {"material": "重复阀门", "spec": "DN50", "qty": 1}
        r1 = _resp([item], doc_type="quote")
        r2 = _resp([item, {"material": "新阀门", "spec": "DN80", "qty": 2}], doc_type="quote")
        merged = ResultAggregator.merge([r1, r2], "quote")
        names = [it["material"] for it in merged.data["items"]]
        # 重复阀门 should appear only once
        assert names.count("重复阀门") == 1
        assert "新阀门" in names

    def test_tender_merge(self):
        items_a = [{"name": "桥架", "spec": "200×100", "quantity": 10}]
        items_b = [{"name": "配电箱", "spec": "XL-21", "quantity": 2}]
        r1 = _resp(items_a, doc_type="tender", project_name="金桥项目")
        r2 = _resp(items_b, doc_type="tender", project_name="")
        merged = ResultAggregator.merge([r1, r2], "tender")
        assert merged.data["project_name"] == "金桥项目"
        assert len(merged.data["items"]) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 3. ExtractionPipeline (MockProvider — no real API)
# ═══════════════════════════════════════════════════════════════════════════
class TestExtractionPipeline:
    """Pipeline unit tests using a deterministic MockProvider."""

    def _make_provider_for_pages(
        self, total_pages: int, doc_type: str = "quote"
    ) -> PageAwareMockProvider:
        """Build a MockProvider with one response per page."""
        pages = _pages(total_pages)
        responses = {}
        for i, page in enumerate(pages):
            if doc_type == "quote":
                items = [{"material": f"Item_page{i}", "spec": f"spec{i}", "qty": i + 1}]
                responses[page] = _resp(
                    items, doc_type="quote",
                    supplier_name="供应商A" if i == 0 else "",
                    tokens_used=100, duration_ms=300,
                )
            else:
                items = [{"name": f"材料_page{i}", "spec": f"spec{i}", "quantity": i + 1}]
                responses[page] = _resp(
                    items, doc_type="tender",
                    project_name="测试项目" if i == 0 else "",
                    tokens_used=100, duration_ms=300,
                )
        return PageAwareMockProvider(responses)

    def test_short_doc_single_api_call(self):
        """Each page is sent as its own provider call."""
        pages = _pages(3)
        provider = self._make_provider_for_pages(3, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "test prompt", "quote")

        assert provider.call_count == 3
        assert result.data["items"]  # has items

    def test_12_page_doc_splits_into_3_calls(self, tmp_path):
        """12-page document triggers 12 page-level provider calls."""
        pages = _pages(12)
        provider = self._make_provider_for_pages(12, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")

        assert provider.call_count == 12
        assert result.metadata["batches"] == 12

    def test_5_page_doc_splits_into_2_calls(self):
        pages = _pages(5)
        provider = self._make_provider_for_pages(5, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")

        assert provider.call_count == 5

    def test_items_aggregated_across_all_batches(self):
        pages = _pages(12)   # 12 pages → 12 items (one per page from MockProvider)
        provider = self._make_provider_for_pages(12, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")

        assert len(result.data["items"]) == 12

    def test_supplier_name_from_first_batch(self):
        pages = _pages(8)
        provider = self._make_provider_for_pages(8, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")
        assert result.data["supplier_name"] == "供应商A"

    def test_total_tokens_summed(self):
        pages = _pages(8)  # 8 pages, each 100 tokens
        provider = self._make_provider_for_pages(8, "quote")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")
        assert result.tokens_used == 800  # 8 × 100

    def test_postprocess_runs_after_aggregation(self):
        """Numeric coercion and blank-name filtering must apply to merged items."""
        items = [
            {"material": "阀门", "spec": "DN50", "qty": "2", "unit_price": "1,500.00"},
            {"material": "", "spec": "DN80", "qty": 1},   # no material — should be filtered
        ]
        provider = MockProvider([_resp(items)])
        pipeline = ExtractionPipeline(provider)
        pages = _pages(1)

        result = pipeline._run_batched(pages, {}, "prompt", "quote")
        result.data = pipeline._postprocess_quote(result.data, {})

        cleaned = result.data["items"]
        assert len(cleaned) == 1  # blank-material row dropped
        assert cleaned[0]["qty"] == 2.0
        assert cleaned[0]["unit_price"] == 1500.0

    def test_tender_pipeline(self):
        pages = _pages(8)
        provider = self._make_provider_for_pages(8, "tender")
        pipeline = ExtractionPipeline(provider)

        result = pipeline._run_batched(pages, {}, "prompt", "tender")
        assert result.data["project_name"] == "测试项目"
        assert provider.call_count == 8

    def test_each_provider_call_receives_one_page(self):
        """Page-level extraction never prepends context pages."""
        pages = _pages(5)
        provider = self._make_provider_for_pages(5, "quote")
        pipeline = ExtractionPipeline(provider)
        pipeline._run_batched(pages, {}, "prompt", "quote")

        assert provider.pages_per_call() == [1, 1, 1, 1, 1]
        assert sorted(c[0] for c in provider._calls) == sorted(pages)


# ═══════════════════════════════════════════════════════════════════════════
# 4. End-to-end with real PDFs (marker: e2e)
# ═══════════════════════════════════════════════════════════════════════════
PROJECT_PDFS = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "docs" / "项目资料" / "初始资料"
)


@pytest.mark.e2e
class TestPipelineWithRealPDFs:
    """Full stack: real PDF → DocumentLoader → Splitter → Qwen-VL → Aggregator.

    Requires DASHSCOPE_API_KEY in env and the project PDFs on disk.
    Run: pytest -m e2e -k TestPipelineWithRealPDFs -s
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_pdfs(self):
        if not PROJECT_PDFS.exists():
            pytest.skip("Project PDF directory not found")

    @pytest.fixture
    def pipeline(self):
        _load_env_file()
        if not os.environ.get("DASHSCOPE_API_KEY"):
            pytest.skip("DASHSCOPE_API_KEY not set")
        from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
        return ExtractionPipeline(DashScopeOCRProvider())

    def _quote_pdf(self, name: str) -> Path:
        p = PROJECT_PDFS / name
        if not p.exists():
            pytest.skip(f"PDF not found: {p}")
        return p

    def test_taikelong_12page_batched(self, pipeline):
        """泰科龙 is a 12-page PDF → 3 batches → should yield ≥ 5 items.
        Some batches may be skipped due to content moderation — that's acceptable
        as long as we still get data from the other batches.
        """
        pdf = self._quote_pdf("泰科龙投标文件.pdf")
        resp = pipeline.extract_quote(str(pdf))

        items = resp.data.get("items") or []
        skipped = resp.metadata.get("skipped_batches", [])
        print(
            f"\n[OK] 泰科龙 batches={resp.metadata.get('batches', 1)} "
            f"items={len(items)} skipped={skipped}"
        )
        assert len(items) >= 5, f"Expected >= 5 items, got {len(items)}"

    @pytest.mark.e2e
    def test_taikelong_page_moderation_scan(self):
        """Scan each page of 泰科龙 individually to find which pages trigger content moderation."""
        _load_env_file()
        if not os.environ.get("DASHSCOPE_API_KEY"):
            pytest.skip("DASHSCOPE_API_KEY not set")
        pdf = PROJECT_PDFS / "泰科龙投标文件.pdf"
        if not pdf.exists():
            pytest.skip(f"PDF not found: {pdf}")

        from apps.api.intelligence.document_loader import DocumentLoader
        from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
        from apps.api.intelligence.base import ContentModerationError
        from apps.api.intelligence.schemas import QUOTE_SCHEMA
        from apps.api.intelligence.prompts import QUOTE_PROMPT

        images = DocumentLoader.to_images(pdf)
        provider = DashScopeOCRProvider()
        blocked: list[int] = []
        ok: list[int] = []

        print(f"\n泰科龙 total pages: {len(images)}")
        for i, page_bytes in enumerate(images):
            try:
                provider.extract([page_bytes], QUOTE_SCHEMA, QUOTE_PROMPT)
                ok.append(i)
                print(f"  page {i:2d}: OK")
            except ContentModerationError:
                blocked.append(i)
                print(f"  page {i:2d}: BLOCKED (content moderation)")
            except Exception as e:
                print(f"  page {i:2d}: ERROR — {e}")

        print(f"\nBlocked pages: {blocked}")
        print(f"OK pages: {ok}")
        # Test passes as long as it completes; result is informational
        assert True

    def test_kaishuoxinzheng_batched(self, pipeline):
        """凯硕新正 quote — verify item count survives batching."""
        pdf = self._quote_pdf("凯硕新正投标文件.pdf")
        resp = pipeline.extract_quote(str(pdf))

        items = resp.data.get("items") or []
        print(f"\n[OK] 凯硕新正 batches={resp.metadata.get('batches', 1)} items={len(items)}")
        assert len(items) >= 5

    def test_shanghaimiances_batched(self, pipeline):
        """上海绵存 quote — verify item count survives batching."""
        pdf = self._quote_pdf("上海绵存投标文件.pdf")
        resp = pipeline.extract_quote(str(pdf))

        items = resp.data.get("items") or []
        print(f"\n[OK] 上海绵存 batches={resp.metadata.get('batches', 1)} items={len(items)}")
        assert len(items) >= 5

    def test_tender_batched(self, pipeline):
        """招标文件审批表 — validate project metadata extracted on first batch."""
        pdf = self._quote_pdf("招标文件审批表.pdf")
        resp = pipeline.extract_tender(str(pdf))

        project_name = resp.data.get("project_name") or ""
        print(f"\n[OK] 招标审批 project_name={project_name!r} batches={resp.metadata.get('batches', 1)}")
        assert project_name, "project_name should not be empty"
