"""Test Qwen-VL-OCR table_parsing on docs/test PDFs.

Two-stage pipeline prototype:
  Stage 1: Qwen-VL-OCR (table_parsing) -> HTML tables per page
  Stage 2: (future) Text LLM -> structured JSON

This script tests Stage 1 only — outputs raw HTML table results.
"""
from __future__ import annotations
import base64, csv, io, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

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

import dashscope

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
dashscope.api_key = DASHSCOPE_API_KEY

RENDER_SCALE = 2.0
MAX_EDGE_PX = 2400
CONCURRENCY = 8

TEST_DIR = ROOT / "docs" / "test"
PDFS = [
    TEST_DIR / "泰科龙投标文件.pdf",
    TEST_DIR / "凯硕新正投标文件.pdf",
    TEST_DIR / "上海绵存投标文件.pdf",
    TEST_DIR / "徐汇区华泾镇D5B一期桥架上海浩财实业有限公司桥架报价清单9页.pdf",
]

OUT_DIR = ROOT / "data" / "ocr_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def render_pdf(path: Path) -> list[bytes]:
    """Render ALL pages of a PDF to PNG bytes (no page cap for testing)."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        images = []
        for i in range(len(pdf)):
            page = pdf[i]
            pil_img = page.render(scale=RENDER_SCALE).to_pil()
            w, h = pil_img.size
            longest = max(w, h)
            if longest > MAX_EDGE_PX:
                scale = MAX_EDGE_PX / longest
                pil_img = pil_img.resize(
                    (int(w * scale), int(h * scale)), Image.LANCZOS
                )
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG", optimize=True)
            images.append(buf.getvalue())
        return images
    finally:
        pdf.close()


def ocr_page(page_bytes: bytes, page_idx: int) -> dict:
    """Call Qwen-VL-OCR table_parsing on a single page image."""
    b64 = base64.b64encode(page_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"

    t0 = time.time()
    try:
        resp = dashscope.MultiModalConversation.call(
            model="qwen-vl-ocr-latest",
            messages=[{
                "role": "user",
                "content": [{
                    "image": data_uri,
                    "min_pixels": 3136,
                    "max_pixels": 8388608,
                }],
            }],
            ocr_options={"task": "table_parsing"},
        )
        elapsed = time.time() - t0

        if resp.status_code != 200:
            return {
                "page": page_idx + 1,
                "ok": False,
                "error": f"HTTP {resp.status_code}: {resp.message}",
                "elapsed": elapsed,
                "html": "",
                "tokens": 0,
            }

        text = ""
        tokens = 0
        if resp.output and resp.output.choices:
            choice = resp.output.choices[0]
            if choice.message and choice.message.content:
                for part in choice.message.content:
                    if hasattr(part, "text"):
                        text += part.text
                    elif isinstance(part, dict) and "text" in part:
                        text += part["text"]
        if resp.usage:
            tokens = getattr(resp.usage, "total_tokens", 0) or 0

        return {
            "page": page_idx + 1,
            "ok": True,
            "html": text,
            "elapsed": elapsed,
            "tokens": tokens,
            "error": "",
        }
    except Exception as e:
        return {
            "page": page_idx + 1,
            "ok": False,
            "error": str(e),
            "elapsed": time.time() - t0,
            "html": "",
            "tokens": 0,
        }


def process_pdf(pdf_path: Path) -> list[dict]:
    """Render + OCR all pages of a PDF concurrently."""
    print(f"\n{'='*80}", flush=True)
    print(f"[PDF] {pdf_path.name}", flush=True)
    print(f"  Rendering pages...", flush=True)

    t0 = time.time()
    images = render_pdf(pdf_path)
    render_time = time.time() - t0
    print(f"  {len(images)} pages rendered in {render_time:.1f}s", flush=True)

    print(f"  OCR with concurrency={CONCURRENCY}...", flush=True)
    results = [None] * len(images)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(ocr_page, img, idx): idx
            for idx, img in enumerate(images)
        }
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            r = fut.result()
            results[idx] = r
            done += 1
            status = "OK" if r["ok"] else f"FAIL: {r['error'][:60]}"
            print(
                f"    page {r['page']:>3}/{len(images)}  "
                f"{r['elapsed']:>5.1f}s  {r['tokens']:>5} tok  {status}",
                flush=True,
            )

    total_time = time.time() - t0
    ok_count = sum(1 for r in results if r["ok"])
    total_tokens = sum(r["tokens"] for r in results)
    print(
        f"  TOTAL: {ok_count}/{len(images)} pages OK, "
        f"{total_tokens} tokens, {total_time:.1f}s",
        flush=True,
    )
    return results


def save_results(pdf_path: Path, results: list[dict]):
    """Save raw HTML output per page to a text file for review."""
    safe_name = pdf_path.stem[:40]
    out_path = OUT_DIR / f"{safe_name}__ocr.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"PDF: {pdf_path.name}\n")
        f.write(f"Pages: {len(results)}\n")
        f.write(f"OK: {sum(1 for r in results if r['ok'])}\n")
        f.write(f"Total tokens: {sum(r['tokens'] for r in results)}\n\n")
        for r in results:
            f.write(f"{'='*60}\n")
            f.write(f"Page {r['page']}  |  {r['elapsed']:.1f}s  |  {r['tokens']} tokens\n")
            if r["ok"]:
                f.write(r["html"])
            else:
                f.write(f"ERROR: {r['error']}")
            f.write("\n\n")
    print(f"  -> {out_path.relative_to(ROOT)}", flush=True)


# ── main ──
print("="*80, flush=True)
print("Qwen-VL-OCR table_parsing test", flush=True)
print(f"API key: ...{DASHSCOPE_API_KEY[-6:]}", flush=True)
print(f"Concurrency: {CONCURRENCY}", flush=True)
print("="*80, flush=True)

summary = []
for pdf in PDFS:
    if not pdf.exists():
        print(f"\n  SKIP (not found): {pdf.name}", flush=True)
        continue
    results = process_pdf(pdf)
    save_results(pdf, results)
    summary.append({
        "pdf": pdf.name,
        "pages": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "tokens": sum(r["tokens"] for r in results),
        "time": sum(r["elapsed"] for r in results),
        "wall": results[-1]["elapsed"] if results else 0,  # rough
    })

print(f"\n\n{'='*80}", flush=True)
print(f"{'SUMMARY':^80}", flush=True)
print(f"{'='*80}", flush=True)
print(f"  {'PDF':<45} {'pages':>5} {'ok':>4} {'tokens':>7} {'time':>7}", flush=True)
print(f"  {'-'*72}", flush=True)
for s in summary:
    print(
        f"  {s['pdf']:<45} {s['pages']:>5} {s['ok']:>4} "
        f"{s['tokens']:>7} {s['time']:>6.0f}s",
        flush=True,
    )
print(flush=True)
