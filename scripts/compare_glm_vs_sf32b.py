"""GLM-4.6V vs Qwen3-VL-32B quote item comparison.

Runs both models sequentially on all test PDFs, outputs full item lists.
"""
from __future__ import annotations
import csv, os, sys, json, time
from pathlib import Path

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

from apps.api.intelligence.pipeline import ExtractionPipeline
from apps.api.intelligence.providers.siliconflow import SiliconFlowProvider
import apps.api.intelligence.pipeline as pipeline_mod

CONFIGS = [
    {
        "tag": "glm-4.6v",
        "api_key": os.environ.get("GLM_API_KEY", ""),
        "base_url": os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
        "model": "glm-4.6v",
        "concurrency": 6,
    },
    {
        "tag": "Qwen3-VL-32B",
        "api_key": os.environ.get("SILICONFLOW_API_KEY", ""),
        "base_url": os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        "model": "Qwen/Qwen3-VL-32B-Instruct",
        "concurrency": 4,
    },
]

TEST_DIR = ROOT / "tests" / "fixtures" / "documents" / "bid"
TEST_DIR_OTHER = ROOT / "docs" / "test"  # design/28 不迁移的其他材料类别夹具
PDFS = [
    TEST_DIR / "泰科龙投标文件.pdf",
    TEST_DIR / "凯硕新正投标文件.pdf",
    TEST_DIR / "上海绵存投标文件.pdf",
    TEST_DIR_OTHER / "徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页.pdf",
]


def guess_type(p: Path) -> str:
    return "quote" if "报价" in p.name else "tender"


def extract(cfg: dict, pdf: Path, doc_type: str) -> dict:
    pipeline_mod.PAGE_CONCURRENCY = cfg["concurrency"]
    provider = SiliconFlowProvider(
        api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
    )
    pipeline = ExtractionPipeline(provider)
    t0 = time.time()
    try:
        if doc_type == "tender":
            resp = pipeline.extract_tender(str(pdf))
        else:
            resp = pipeline.extract_quote(str(pdf))
        items = (resp.data or {}).get("items") or []
        skipped = (resp.metadata or {}).get("skipped_pages") or []
        return {
            "ok": True, "items": items, "elapsed": time.time() - t0,
            "tokens": resp.tokens_used or 0, "skipped": skipped,
        }
    except Exception as e:
        return {"ok": False, "items": [], "elapsed": time.time() - t0,
                "tokens": 0, "skipped": [], "error": str(e)}


CSV_DIR = ROOT / "data" / "model_compare"
CSV_DIR.mkdir(parents=True, exist_ok=True)

TENDER_FIELDS = ["name", "category", "spec", "unit", "quantity", "remark"]
QUOTE_FIELDS  = ["material", "spec", "brand", "unit", "qty", "unit_price", "total_price", "tax_rate", "remark"]


def save_csv(tag: str, pdf: Path, doc_type: str, items: list[dict], elapsed: float, skipped: list):
    """Save extraction result to CSV for review."""
    safe_pdf = pdf.stem[:30]
    safe_tag = tag.replace("/", "_").replace(" ", "_")
    csv_path = CSV_DIR / f"{safe_pdf}__{safe_tag}.csv"
    fields = QUOTE_FIELDS if doc_type == "quote" else TENDER_FIELDS
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#"] + fields)
        for i, it in enumerate(items, 1):
            w.writerow([i] + [it.get(k, "") for k in fields])
        w.writerow([])
        w.writerow(["model", tag])
        w.writerow(["file", pdf.name])
        w.writerow(["items", len(items)])
        w.writerow(["elapsed_s", f"{elapsed:.1f}"])
        w.writerow(["skipped_pages", len(skipped)])
    print(f"  -> CSV: {csv_path.relative_to(ROOT)}", flush=True)


def short_name(it: dict, doc_type: str) -> str:
    if doc_type == "quote":
        m = (it.get("material") or "").strip()
        s = (it.get("spec") or "").strip()
        q = it.get("qty")
        p = it.get("unit_price")
        return f"{m} | {s} | qty={q} | price={p}"
    else:
        n = (it.get("name") or "").strip()
        s = (it.get("spec") or "").strip()
        q = it.get("quantity")
        return f"{n} | {s} | qty={q}"


# ── main ──
print("=" * 90, flush=True)
print(f"{'GLM-4.6V vs Qwen3-VL-32B':^90}", flush=True)
print("=" * 90, flush=True)

summary = []

for pdf in PDFS:
    if not pdf.exists():
        continue
    doc_type = guess_type(pdf)
    print(f"\n{'='*90}", flush=True)
    print(f"[PDF] {pdf.name}  [{doc_type}]", flush=True)
    print("-" * 90, flush=True)

    results = {}
    for cfg in CONFIGS:
        tag = cfg["tag"]
        print(f"\n  >> {tag} (model={cfg['model']}, concurrency={cfg['concurrency']})", flush=True)
        r = extract(cfg, pdf, doc_type)
        results[tag] = r
        if r["ok"]:
            skip_info = f", skipped={len(r['skipped'])}" if r["skipped"] else ""
            print(f"  {tag}: {r['elapsed']:.1f}s | {len(r['items'])} items | {r['tokens']} tokens{skip_info}", flush=True)
            save_csv(tag, pdf, doc_type, r["items"], r["elapsed"], r["skipped"])
        else:
            print(f"  {tag}: FAILED {r['elapsed']:.1f}s -- {r.get('error','')}", flush=True)

    # Side-by-side item listing
    r1 = results.get("glm-4.6v", {})
    r2 = results.get("Qwen3-VL-32B", {})
    if r1.get("ok") and r2.get("ok"):
        a, b = r1["items"], r2["items"]
        print(f"\n  {'#':<4} {'glm-4.6v':<43} {'Qwen3-VL-32B'}", flush=True)
        print(f"  {'-'*86}", flush=True)
        maxlen = max(len(a), len(b))
        for i in range(maxlen):
            ai = short_name(a[i], doc_type) if i < len(a) else ""
            bi = short_name(b[i], doc_type) if i < len(b) else ""
            print(f"  {i+1:<4} {ai:<43} {bi}", flush=True)

        summary.append({
            "pdf": pdf.name,
            "glm_items": len(a), "glm_skip": len(r1["skipped"]), "glm_time": r1["elapsed"],
            "sf_items": len(b), "sf_skip": len(r2["skipped"]), "sf_time": r2["elapsed"],
        })

# Final summary table
print(f"\n\n{'='*90}", flush=True)
print(f"{'SUMMARY':^90}", flush=True)
print(f"{'='*90}", flush=True)
print(f"  {'PDF':<40} {'glm-4.6v':>12} {'skip':>5} {'time':>7}  {'Qwen3-32B':>12} {'skip':>5} {'time':>7}", flush=True)
print(f"  {'-'*86}", flush=True)
for s in summary:
    print(f"  {s['pdf']:<40} {s['glm_items']:>8} items {s['glm_skip']:>5} {s['glm_time']:>6.0f}s"
          f"  {s['sf_items']:>8} items {s['sf_skip']:>5} {s['sf_time']:>6.0f}s", flush=True)
print(flush=True)
