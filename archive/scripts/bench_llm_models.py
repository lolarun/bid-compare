"""Benchmark different qwen3 text models for Stage 2 (per-page JSON extraction).

Uses pre-cached OCR results from data/ocr_test/ to avoid re-running OCR.
Tests on the quote PDF (9 pages, most items).
"""
from __future__ import annotations
import csv, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

env_file = ROOT / "apps" / "api" / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)

MODELS = [
    "qwen3-8b",
    "qwen3-32b",
    "qwen3.5-flash",
    "qwen3.6-flash",
]

LLM_CONCURRENCY = 20

STAGE2_QUOTE_PROMPT = """你是机电材料报价单解析助理。下面是OCR识别出的HTML表格内容。
请从中提取报价明细，返回严格的JSON格式。

要求：
- 只提取材料报价行，不要表头、合计行、小计行
- 区分 unit_price（含税单价）与 unit_price_excl_tax（不含税单价）
- 总价若已标注使用原值，否则留null
- 税率用小数如0.13表示13%
- 品牌按原文
- 无法识别的字段返回空字符串或null

返回JSON格式：
{"supplier_name": "供应商名称", "items": [{"material": "材料名称", "spec": "规格型号", "brand": "品牌", "unit": "单位", "qty": 数量, "unit_price": 含税单价, "unit_price_excl_tax": 不含税单价, "total_price": 总价, "tax_rate": 税率小数, "remark": "备注"}]}

如果该页没有报价明细（如封面、证书等非报价页），返回 {"items": []}"""


def load_ocr_pages() -> list[str]:
    """Load cached OCR HTML for the quote PDF."""
    ocr_file = ROOT / "data" / "ocr_test" / "徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页__ocr.txt"
    text = ocr_file.read_text(encoding="utf-8")
    pages = []
    parts = text.split("=" * 60)
    for part in parts[1:]:  # skip header
        lines = part.strip().split("\n")
        if not lines:
            continue
        # First line is "Page X | ..."
        html = "\n".join(lines[1:]).strip()
        if html:
            pages.append(html)
    return pages


def llm_parse_page(client: OpenAI, model: str, html: str, page_idx: int) -> dict:
    t0 = time.time()
    try:
        extra = {}
        if "qwen3" in model and "flash" not in model:
            extra["extra_body"] = {"enable_thinking": False}

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": STAGE2_QUOTE_PROMPT},
                {"role": "user", "content": f"以下是第{page_idx+1}页的OCR结果：\n\n{html}"},
            ],
            temperature=0.1,
            max_tokens=8192,
            **extra,
        )
        elapsed = time.time() - t0
        raw = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0

        raw_clean = raw.strip()
        if raw_clean.startswith("```"):
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean)
            raw_clean = re.sub(r"\s*```$", "", raw_clean)
        if "</think>" in raw_clean:
            raw_clean = raw_clean.split("</think>")[-1].strip()
        if raw_clean.startswith("```"):
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean)
            raw_clean = re.sub(r"\s*```$", "", raw_clean)

        data = json.loads(raw_clean)
        items = data.get("items") or []
        return {"page": page_idx + 1, "ok": True, "items": items,
                "elapsed": elapsed, "tokens": tokens}
    except Exception as e:
        return {"page": page_idx + 1, "ok": False, "items": [],
                "elapsed": time.time() - t0, "tokens": 0, "error": str(e)[:80]}


def test_model(model: str, pages: list[str]) -> dict:
    print(f"\n  [{model}] concurrency={LLM_CONCURRENCY}, {len(pages)} pages", flush=True)
    client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)

    t0 = time.time()
    results = [None] * len(pages)

    with ThreadPoolExecutor(max_workers=LLM_CONCURRENCY) as pool:
        futures = {
            pool.submit(llm_parse_page, client, model, html, i): i
            for i, html in enumerate(pages)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            r = fut.result()
            results[idx] = r
            status = f"{len(r['items'])} items" if r.get("ok") else f"FAIL:{r.get('error','')[:40]}"
            print(f"    p{r['page']}: {r['elapsed']:>5.1f}s {r['tokens']:>5}tok {status}", flush=True)

    wall = time.time() - t0
    total_items = sum(len(r["items"]) for r in results if r)
    total_tokens = sum(r["tokens"] for r in results if r)
    fails = sum(1 for r in results if r and not r.get("ok"))
    print(f"  [{model}] {total_items} items | {wall:.0f}s wall | {total_tokens} tok | {fails} fails", flush=True)

    return {
        "model": model, "items": total_items, "wall": wall,
        "tokens": total_tokens, "fails": fails, "results": results,
    }


# ── Main ──
print("=" * 80, flush=True)
print("LLM model benchmark — per-page parallel, quote PDF", flush=True)
print(f"Models: {', '.join(MODELS)}", flush=True)
print(f"Concurrency: {LLM_CONCURRENCY}", flush=True)
print("=" * 80, flush=True)

pages = load_ocr_pages()
print(f"Loaded {len(pages)} OCR pages", flush=True)

summary = []
for model in MODELS:
    r = test_model(model, pages)
    summary.append(r)

print(f"\n\n{'='*80}", flush=True)
print(f"{'BENCHMARK RESULTS':^80}", flush=True)
print(f"{'='*80}", flush=True)
print(f"  {'Model':<25} {'Items':>6} {'Wall':>6} {'Tokens':>8} {'Fails':>6}", flush=True)
print(f"  {'-'*55}", flush=True)
for s in summary:
    print(f"  {s['model']:<25} {s['items']:>6} {s['wall']:>5.0f}s {s['tokens']:>8} {s['fails']:>6}", flush=True)
print(flush=True)
