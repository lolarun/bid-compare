"""Test ZhipuAI GLM vision models on docs/test PDFs.

Usage:
    python scripts/test_glm.py [--pdf docs/test/xxx.pdf]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Load .env
env_file = ROOT / "apps" / "api" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.siliconflow import SiliconFlowProvider
import apps.api.intelligence.pipeline as pipeline_mod

GLM_API_KEY = "ec621804c60d48209b8d5fd7d7340f68.mIun53WisL2y7alG"
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# glm-4.6v = flagship; glm-5v-turbo = larger context (200K), newer gen
# glm-4.6v-flash / glm-4.6v-flashx = faster/cheaper variants
MODELS = [
    ("glm-4.6v",       "glm-4.6v",        6),
    ("glm-5v-turbo",   "glm-5v-turbo",     6),
    ("glm-4.6v-flash", "glm-4.6v-flash",  10),
    ("glm-4.6v-flashx","glm-4.6v-flashx", 10),
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
        api_key=GLM_API_KEY,
        base_url=GLM_BASE_URL,
        model=model,
    )
    pipeline = ExtractionPipeline(provider)

    t0 = time.time()

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


def print_result(tag: str, result: dict):
    if result["ok"]:
        skip_str = str(result["skipped"]) if result["skipped"] else "无"
        print(f"  {tag}: {result['elapsed']:.1f}s | {result['items']} items | "
              f"{result['tokens']} tokens | skipped={skip_str}")
        for i, item in enumerate(result["sample"], 1):
            name = item.get("name") or item.get("material") or "?"
            spec = item.get("spec") or ""
            qty  = item.get("quantity") or item.get("qty") or ""
            print(f"    [{i}] {name}  {spec}  qty={qty}")
    else:
        print(f"  {tag}: FAILED {result['elapsed']:.1f}s -- {result['error']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="Single PDF to test")
    args = parser.parse_args()

    pdfs = [Path(args.pdf)] if args.pdf else DEFAULT_PDFS
    pdfs = [p for p in pdfs if p.exists()]
    if not pdfs:
        print("No PDFs found")
        sys.exit(1)

    print("=" * 70)
    print(f"{'GLM VISION MODEL TEST':^70}")
    print("=" * 70)

    for pdf in pdfs:
        doc_type = guess_doc_type(pdf)
        print(f"\n[PDF] {pdf.name}  [{doc_type}]")
        print("-" * 70)

        results = {}
        for tag, model, concurrency in MODELS:
            print(f"\n  >> {tag} (concurrency={concurrency})")
            results[tag] = run_one(pdf, model, concurrency, doc_type)
            print_result(tag, results[tag])

        ok_results = [(tag, r) for tag, r in results.items() if r["ok"]]
        if len(ok_results) > 1:
            print(f"\n  -- Summary --")
            best_speed = min(ok_results, key=lambda x: x[1]["elapsed"])
            best_items = max(ok_results, key=lambda x: x[1]["items"])
            print(f"  最快: {best_speed[0]} ({best_speed[1]['elapsed']:.1f}s)")
            print(f"  最多条数: {best_items[0]} ({best_items[1]['items']} items)")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
