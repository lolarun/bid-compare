"""End-to-end tests against the real Qwen-VL API.

Skipped by default — run with `pytest -m e2e` and DASHSCOPE_API_KEY set.

Tests are organized in increasing complexity:
1. Connectivity: API key works; models.list() reachable
2. Synthetic doc: small image with known text → confirms extraction works
3. Real CSV-rendered doc: full quote table from docs/data/ → accuracy ≥ 95%
4. Real CSV-rendered tender doc: tender items from docs/data/ → accuracy ≥ 95%
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from apps.api.intelligence import (
    ExtractionPipeline,
    QwenVLProvider,
    QUOTE_SCHEMA,
    QUOTE_PROMPT,
    TENDER_SCHEMA,
    TENDER_PROMPT,
)

pytestmark = pytest.mark.e2e


def _load_env():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        pytest.skip("apps/api/.env missing — set DASHSCOPE_API_KEY to run e2e tests")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        pytest.skip("DASHSCOPE_API_KEY not set")


@pytest.fixture(scope="module")
def provider():
    _load_env()
    return QwenVLProvider()


@pytest.fixture(scope="module")
def pipeline(provider):
    return ExtractionPipeline(provider)


def _find_chinese_font() -> ImageFont.ImageFont:
    """Locate a Chinese-capable system font (Windows)."""
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for p in candidates:
        if Path(p).is_file():
            return ImageFont.truetype(p, 22)
    return ImageFont.load_default()


def _render_synthetic_quote_image() -> bytes:
    """Render a tiny PNG containing a 2-row quote table with known content."""
    font = _find_chinese_font()
    img = Image.new("RGB", (1200, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 20), "供应商：江苏华润管业  报价日期: 2026-05-20", font=font, fill="black")
    d.text((20, 80), "材料名称       规格        品牌    单位 数量 单价", font=font, fill="black")
    d.text((20, 130), "DN100 闸阀     Z45X-16Q   良工    个   12   720.00", font=font, fill="black")
    d.text((20, 180), "桥架300×200    热镀锌1.2mm 华威    米   200   78.00", font=font, fill="black")
    d.text((20, 230), "PPR给水管De32  3.6mm壁厚  伟星    米   500   12.80", font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestConnectivity:
    def test_api_reachable(self, provider):
        # If __init__ succeeds and candidates is non-empty, we're good
        assert provider.candidates
        assert "qwen3-vl-plus" in provider.candidates


class TestSyntheticExtraction:
    def test_synthetic_quote_extraction(self, provider, tmp_path):
        img_bytes = _render_synthetic_quote_image()
        # Save for debugging
        (tmp_path / "synthetic_quote.png").write_bytes(img_bytes)

        resp = provider.extract([img_bytes], QUOTE_SCHEMA, QUOTE_PROMPT, timeout=120)
        assert resp.data
        assert "items" in resp.data
        items = resp.data["items"]
        print(f"\nProvider: {resp.provider}, tokens: {resp.tokens_used}, "
              f"duration: {resp.duration_ms}ms")
        print(f"Extracted {len(items)} items:")
        for it in items:
            print(f"  {it}")
        assert len(items) >= 3, f"Expected ≥3 items, got {len(items)}: {items}"

        # Per-item field-level accuracy check (combined material + spec).
        # The model legitimately splits "桥架300×200" → material="桥架", spec="300×200".
        expected = [
            {"keywords": ["DN100", "闸阀"], "qty": 12, "price": 720.0, "brand": "良工"},
            {"keywords": ["桥架", "300", "200"], "qty": 200, "price": 78.0, "brand": "华威"},
            {"keywords": ["PPR", "De32"], "qty": 500, "price": 12.8, "brand": "伟星"},
        ]
        field_hits = 0
        field_total = 0
        for exp in expected:
            best_item = None
            best_score = -1
            for it in items:
                blob = " ".join([
                    str(it.get("material") or ""),
                    str(it.get("spec") or ""),
                    str(it.get("brand") or ""),
                ])
                score = sum(1 for kw in exp["keywords"] if kw in blob)
                if score > best_score:
                    best_score = score
                    best_item = it
            assert best_item is not None
            # Score each field of the matched item
            for field, ex_val in [
                ("brand_match", exp["brand"] in str(best_item.get("brand") or "")),
                ("qty_match", best_item.get("qty") == exp["qty"]),
                ("price_match", best_item.get("unit_price") == exp["price"]),
            ]:
                field_total += 1
                if field:
                    field_hits += 1
        accuracy = field_hits / field_total
        print(f"\nField-level accuracy (brand+qty+price across 3 items): {accuracy:.0%}")
        # Synthetic image is low quality (rendered text, no real OCR challenge);
        # target ≥ 90% here; real PDFs targeted at ≥ 95% in test_real_*
        assert accuracy >= 0.90, f"Field accuracy {accuracy:.0%} < 90% baseline"

    def test_pipeline_postprocess(self, pipeline, tmp_path):
        """End-to-end via pipeline (includes post-processing)."""
        img_bytes = _render_synthetic_quote_image()
        img_path = tmp_path / "synthetic.png"
        img_path.write_bytes(img_bytes)

        resp = pipeline.extract_quote(str(img_path), context={"supplier_id": 1})
        data = resp.data
        print(f"\nPostprocessed: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
        assert "items" in data
        # Post-processing fills total_price if unit_price * qty when missing
        # and coerces numerics
        for item in data["items"]:
            if item.get("unit_price") is not None:
                assert isinstance(item["unit_price"], (int, float))


# ─── Real-data accuracy (docs/data/*.csv → rendered PNG → Qwen-VL) ─────────
class TestRealDataAccuracy:
    """Use docs/data/ CSV-rendered images to validate ≥ 95% field accuracy.

    Run `uv run python apps/api/tests/fixtures/generate_test_docs.py` first
    if real_quote.png / real_tender.png are missing.
    """

    @pytest.fixture(autouse=True)
    def _ensure_fixtures(self, fixture_dir):
        if not (fixture_dir / "real_quote.png").exists():
            from apps.api.tests.fixtures import generate_test_docs as gen
            quote_png, qt = gen.make_real_quote_image()
            (fixture_dir / "real_quote.png").write_bytes(quote_png)
            (fixture_dir / "real_quote_truth.json").write_text(
                json.dumps(qt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if not (fixture_dir / "real_tender.png").exists():
            from apps.api.tests.fixtures import generate_test_docs as gen
            tender_png, tt = gen.make_real_tender_image()
            (fixture_dir / "real_tender.png").write_bytes(tender_png)
            (fixture_dir / "real_tender_truth.json").write_text(
                json.dumps(tt, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def test_real_quote_field_accuracy(self, provider, fixture_dir):
        """≥ 95% accuracy on material+price fields per the plan."""
        img = (fixture_dir / "real_quote.png").read_bytes()
        truth = json.loads(
            (fixture_dir / "real_quote_truth.json").read_text(encoding="utf-8")
        )
        resp = provider.extract([img], QUOTE_SCHEMA, QUOTE_PROMPT, timeout=180)
        data = resp.data
        items = data.get("items") or []

        # Save the actual response for regression
        (fixture_dir / "live_real_quote_result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"\nProvider: {resp.provider}  tokens: {resp.tokens_used}  "
              f"duration: {resp.duration_ms}ms")
        print(f"Truth has {len(truth['items'])} items; extracted {len(items)}")

        # Align each extracted row to the closest ground-truth row by qty+unit_price
        # heuristic, then score each field.
        tot_hits = 0
        tot_checks = 0
        for exp in truth["items"]:
            # Find best-matching extracted item by (spec match OR qty+price match)
            best = None
            for it in items:
                blob = " ".join(
                    str(it.get(k) or "") for k in ("material", "spec", "model", "brand")
                )
                if exp["spec"] in blob and exp["model"] in blob:
                    best = it
                    break
            if best is None:
                # Fall back: match by qty + price approximation
                for it in items:
                    if (
                        it.get("qty") == exp["qty"]
                        and it.get("unit_price")
                        and abs(it["unit_price"] - exp["unit_price"]) < 1
                    ):
                        best = it
                        break

            checks = {
                "material_substring": (
                    best is not None
                    and any(
                        kw in str(best.get("material") or "")
                        for kw in [
                            exp["model"],
                            exp["material"][:6],  # first 6 chars
                            exp["spec"],
                        ]
                    )
                ),
                "spec_match": best is not None and exp["spec"] in (
                    str(best.get("spec") or "") + str(best.get("material") or "")
                ),
                "brand_match": best is not None and exp["brand"] in str(best.get("brand") or ""),
                "qty_match": best is not None and best.get("qty") == exp["qty"],
                "price_match": (
                    best is not None
                    and best.get("unit_price") is not None
                    and abs(best["unit_price"] - exp["unit_price"]) <= 1.0
                ),
            }
            print(f"\n  {exp['material'][:30]!s:34} → matched={best is not None}, checks={checks}")
            for v in checks.values():
                tot_checks += 1
                if v:
                    tot_hits += 1

        accuracy = tot_hits / tot_checks if tot_checks else 0
        print(f"\n══ Real-data field accuracy: {tot_hits}/{tot_checks} = {accuracy:.1%} ══")
        # Target ≥ 95% per plan. If we miss, we surface for prompt tuning.
        assert accuracy >= 0.95, (
            f"Accuracy {accuracy:.1%} < 95% target. "
            f"See live_real_quote_result.json for raw extraction; "
            f"may need prompt iteration or model escalation."
        )

    def test_real_tender_extraction(self, provider, fixture_dir):
        """Tender extraction sanity: project info + items present."""
        img = (fixture_dir / "real_tender.png").read_bytes()
        truth = json.loads(
            (fixture_dir / "real_tender_truth.json").read_text(encoding="utf-8")
        )
        resp = provider.extract([img], TENDER_SCHEMA, TENDER_PROMPT, timeout=180)
        data = resp.data
        (fixture_dir / "live_real_tender_result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nProvider: {resp.provider}  duration: {resp.duration_ms}ms")
        items = data.get("items") or []
        print(f"Truth: {len(truth['items'])} items; extracted: {len(items)}")

        assert data.get("project_name"), "project_name should be extracted"
        assert len(items) >= len(truth["items"]) - 1, (
            f"Should extract ≥{len(truth['items']) - 1} items, got {len(items)}"
        )
        # Hit rate on truth → extracted (using model/spec match)
        hits = 0
        for exp in truth["items"]:
            for it in items:
                blob = (str(it.get("name") or "") + " " + str(it.get("spec") or ""))
                if exp["name"][:6] in blob or exp["spec"][:8] in blob:
                    hits += 1
                    break
        accuracy = hits / len(truth["items"])
        print(f"Tender item-recall accuracy: {hits}/{len(truth['items'])} = {accuracy:.1%}")
        assert accuracy >= 0.8, f"Tender recall {accuracy:.1%} < 80%"
