"""Detailed item-level comparison between glm-4.6v and glm-4.6v-flashx.

Runs both models on a single PDF and prints all items side-by-side.
"""
from __future__ import annotations
import os, sys, time
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

GLM_API_KEY  = "ec621804c60d48209b8d5fd7d7340f68.mIun53WisL2y7alG"
GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

PDF = ROOT / "tests" / "fixtures" / "documents" / "bid" / "泰科龙投标文件.pdf"

MODELS = [
    ("glm-4.6v",        6),
    ("glm-4.6v-flashx", 6),
]


def extract(model, concurrency):
    pipeline_mod.PAGE_CONCURRENCY = concurrency
    provider = SiliconFlowProvider(api_key=GLM_API_KEY, base_url=GLM_BASE_URL, model=model)
    pipeline = ExtractionPipeline(provider)
    t0 = time.time()
    resp = pipeline.extract_tender(str(PDF))
    elapsed = time.time() - t0
    items = (resp.data or {}).get("items") or []
    skipped = (resp.metadata or {}).get("skipped_pages") or []
    return items, elapsed, skipped


def fmt_item(it):
    name  = (it.get("name") or "").strip()
    spec  = (it.get("spec") or "").strip()
    unit  = (it.get("unit") or "").strip()
    qty   = it.get("quantity")
    cat   = (it.get("category") or "").strip()
    return f"{name} | {spec} | {unit} | qty={qty} | cat={cat}"


results = {}
for model, conc in MODELS:
    print(f"Running {model}...")
    items, elapsed, skipped = extract(model, conc)
    results[model] = {"items": items, "elapsed": elapsed, "skipped": skipped}
    print(f"  done: {elapsed:.1f}s, {len(items)} items, skipped={skipped or 'none'}")

print("\n" + "=" * 80)
print(f"  glm-4.6v ({len(results['glm-4.6v']['items'])} items)  vs  "
      f"glm-4.6v-flashx ({len(results['glm-4.6v-flashx']['items'])} items)")
print("=" * 80)

# Print both lists
a = results["glm-4.6v"]["items"]
b = results["glm-4.6v-flashx"]["items"]
maxlen = max(len(a), len(b))

print(f"\n{'#':<4} {'glm-4.6v':<55} {'glm-4.6v-flashx'}")
print("-" * 120)
for i in range(maxlen):
    ai = fmt_item(a[i]) if i < len(a) else "(none)"
    bi = fmt_item(b[i]) if i < len(b) else "(none)"
    mark = "  " if i < len(a) and i < len(b) else ("<<" if i >= len(b) else ">>")
    print(f"{i+1:<4} {ai:<55} {mark}  {bi}")

# Find items only in 4.6v (not in flashx by name)
names_b = {(it.get("name") or "").strip() for it in b}
names_a = {(it.get("name") or "").strip() for it in a}

only_in_a = [it for it in a if (it.get("name") or "").strip() not in names_b]
only_in_b = [it for it in b if (it.get("name") or "").strip() not in names_a]

print(f"\n{'='*80}")
print(f"仅 glm-4.6v 有，flashx 没有 ({len(only_in_a)} 条):")
for it in only_in_a:
    print(f"  - {fmt_item(it)}")

print(f"\n仅 glm-4.6v-flashx 有，4.6v 没有 ({len(only_in_b)} 条):")
for it in only_in_b:
    print(f"  - {fmt_item(it)}")
