"""Compare 8B vs 32B SiliconFlow models on test PDFs.

Usage:
    python scripts/compare_models.py [--pdf docs/test/xxx.pdf]

Runs each test PDF through both models, prints a side-by-side summary.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.siliconflow import SiliconFlowProvider
import apps.api.intelligence.pipeline as pipeline_mod

SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

MODELS = [
    ("8B",  "Qwen/Qwen3-VL-8B-Instruct",  10),
    ("32B", "Qwen/Qwen3-VL-32B-Instruct",  4),
]

TEST_DIR = ROOT / "docs" / "test"
DEFAULT_PDFS = [
    TEST_DIR / "泰科龙投标文件.pdf",
    TEST_DIR / "凯硕新正投标文件.pdf",
    TEST_DIR / "上海绵存投标文件.pdf",
    TEST_DIR / "徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页.pdf",
]


def run_one(pdf_path: Path, model: str, concurrency: int, doc_type: str) -> dict:
    pipeline_mod.PAGE_CONCURRENCY = concurrency
    provider = SiliconFlowProvider(
        api_key=SILICONFLOW_API_KEY,
        base_url=SILICONFLOW_BASE_URL,
        model=model,
    )
    pipeline = ExtractionPipeline(provider)

    t0 = time.time()
    skipped = []

    def progress(stage, pct):
        print(f"    [{pct:3d}%] {stage}")

    try:
        if doc_type == "tender":
            resp = pipeline.extract_tender(str(pdf_path), progress_cb=progress)
        else:
            resp = pipeline.extract_quote(str(pdf_path), progress_cb=progress)
        elapsed = time.time() - t0
        items = (resp.data or {}).get("items") or []
        skipped = (resp.metadata or {}).get("skipped_pages") or []
        return {
            "ok": True,
            "elapsed": elapsed,
            "items": len(items),
            "tokens": resp.tokens_used or 0,
            "skipped": skipped,
            "sample": items[:3],
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed": time.time() - t0,
            "error": str(e),
            "items": 0,
            "tokens": 0,
            "skipped": [],
            "sample": [],
        }


def guess_doc_type(pdf_path: Path) -> str:
    name = pdf_path.name
    if "报价" in name or "quote" in name.lower():
        return "quote"
    return "tender"


def print_result(label: str, result: dict):
    if result["ok"]:
        print(f"  {label}: {result['elapsed']:.1f}s | {result['items']} items | "
              f"{result['tokens']} tokens | skipped={result['skipped'] or '无'}")
        for i, item in enumerate(result["sample"], 1):
            name = item.get("name") or item.get("material") or "?"
            spec = item.get("spec") or ""
            qty  = item.get("quantity") or item.get("qty") or ""
            print(f"    [{i}] {name}  {spec}  qty={qty}")
    else:
        print(f"  {label}: FAILED in {result['elapsed']:.1f}s — {result['error']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="Single PDF to test (default: all test PDFs)")
    args = parser.parse_args()

    # load .env
    env_file = ROOT / "apps" / "api" / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    global SILICONFLOW_API_KEY, SILICONFLOW_BASE_URL
    SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
    SILICONFLOW_BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

    if not SILICONFLOW_API_KEY:
        print("ERROR: SILICONFLOW_API_KEY not set")
        sys.exit(1)

    pdfs = [Path(args.pdf)] if args.pdf else DEFAULT_PDFS
    pdfs = [p for p in pdfs if p.exists()]
    if not pdfs:
        print("No PDFs found")
        sys.exit(1)

    print("=" * 70)
    print(f"{'MODEL COMPARISON':^70}")
    print("=" * 70)

    for pdf in pdfs:
        doc_type = guess_doc_type(pdf)
        print(f"\n[PDF] {pdf.name}  [{doc_type}]")
        print("-" * 70)

        results = {}
        for tag, model, concurrency in MODELS:
            print(f"\n  >> {tag} ({model}, concurrency={concurrency})")
            results[tag] = run_one(pdf, model, concurrency, doc_type)
            print_result(tag, results[tag])

        # side-by-side delta
        r8, r32 = results.get("8B"), results.get("32B")
        if r8 and r32 and r8["ok"] and r32["ok"]:
            delta_items = r32["items"] - r8["items"]
            speedup = r32["elapsed"] / r8["elapsed"] if r8["elapsed"] > 0 else 0
            print(f"\n  ── Delta: 32B vs 8B ──")
            print(f"  速度: 8B 快 {speedup:.1f}x")
            print(f"  条数差: {delta_items:+d} (32B {'多' if delta_items >= 0 else '少'}识别 {abs(delta_items)} 条)")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
