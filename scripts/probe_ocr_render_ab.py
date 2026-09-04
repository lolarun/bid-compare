"""Layer 0 A/B — does higher PDF render quality reduce 形近字 OCR errors?

Renders the same pages at two (scale, max_edge) settings, OCRs each with
Qwen-VL-OCR table_parsing, and counts suspect valve-term occurrences so we can
see whether the bump from 2.0/2400 → 3.0/3600 actually recovers 橡胶瓣/闸阀/etc.

Usage:
    python scripts/probe_ocr_render_ab.py
    python scripts/probe_ocr_render_ab.py --pdf tests/fixtures/documents/金桥地体上盖项目-凯硕新正投标文件.pdf --pages 5,6,7
    python scripts/probe_ocr_render_ab.py --configs 2.0:2400,3.0:3600

Needs DASHSCOPE_API_KEY (read from apps/api/.env).
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
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

dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

# Suspect terms: (canonical correct form, [OCR error variants seen]). We count
# correct vs error hits per render setting — more correct / fewer error = better.
SUSPECT = {
    "橡胶瓣止回阀": ["橡胶脚", "橡胶海", "橡胶辨", "橡胶瓣"],
    "闸阀":        ["阀阀", "闸阀"],
    "减压阀组":     ["减压阀组", "减压阀"],
    "倒流防止器":   ["倒流防止器", "倒流防上器", "例流"],
    "节能消声止回阀": ["消声", "消音", "节能"],
}


def render_page(pdf_path: Path, page_idx: int, scale: float, max_edge: int) -> bytes:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        pil = pdf[page_idx].render(scale=scale).to_pil()
        w, h = pil.size
        longest = max(w, h)
        if longest > max_edge:
            s = max_edge / longest
            pil = pil.resize((int(w * s), int(h * s)), Image.LANCZOS)
        buf = io.BytesIO()
        pil.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), pil.size
    finally:
        pdf.close()


def ocr(page_bytes: bytes) -> tuple[str, int, float]:
    b64 = base64.b64encode(page_bytes).decode("ascii")
    t0 = time.time()
    resp = dashscope.MultiModalConversation.call(
        model="qwen-vl-ocr-latest",
        messages=[{"role": "user", "content": [{
            "image": f"data:image/png;base64,{b64}",
            "min_pixels": 3136, "max_pixels": 8388608,
        }]}],
        ocr_options={"task": "table_parsing"},
    )
    elapsed = time.time() - t0
    if resp.status_code != 200:
        return f"__ERROR__ {resp.status_code}: {resp.message}", 0, elapsed
    text = ""
    if resp.output and resp.output.choices:
        msg = resp.output.choices[0].message
        if msg and msg.content:
            for part in msg.content:
                if hasattr(part, "text"):
                    text += part.text
                elif isinstance(part, dict) and "text" in part:
                    text += part["text"]
    tokens = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
    return text, tokens, elapsed


def count_terms(html: str) -> dict[str, int]:
    return {variant: html.count(variant)
            for variants in SUSPECT.values() for variant in variants}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="tests/fixtures/documents/金桥地体上盖项目-凯硕新正投标文件.pdf")
    ap.add_argument("--pages", default="5,6,7", help="1-based page numbers")
    ap.add_argument("--configs", default="2.0:2400,3.0:3600",
                    help="comma list of scale:max_edge")
    args = ap.parse_args()

    pdf_path = ROOT / args.pdf
    pages = [int(p) - 1 for p in args.pages.split(",")]
    configs = []
    for c in args.configs.split(","):
        scale, max_edge = c.split(":")
        configs.append((float(scale), int(max_edge)))

    out_dir = ROOT / "data" / "ocr_test" / "ab"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80, flush=True)
    print(f"Layer 0 A/B  |  {pdf_path.name}  |  pages {args.pages}", flush=True)
    print(f"API key: ...{dashscope.api_key[-6:]}", flush=True)
    print("=" * 80, flush=True)

    # results[(scale,max_edge)][page] = (html, tokens, elapsed, dims)
    agg: dict[tuple, dict[str, int]] = {}
    for scale, max_edge in configs:
        tag = f"{scale}x_{max_edge}px"
        total_counts: dict[str, int] = {}
        print(f"\n### config scale={scale} max_edge={max_edge}", flush=True)
        for pidx in pages:
            img_bytes, dims = render_page(pdf_path, pidx, scale, max_edge)
            html, tokens, elapsed = ocr(img_bytes)
            (out_dir / f"p{pidx+1}_{tag}.html").write_text(html, encoding="utf-8")
            counts = count_terms(html)
            for k, v in counts.items():
                total_counts[k] = total_counts.get(k, 0) + v
            hits = {k: v for k, v in counts.items() if v}
            print(f"  page {pidx+1:>2}  {dims[0]}x{dims[1]}px  {tokens:>5}tok "
                  f"{elapsed:>5.1f}s  hits={hits}", flush=True)
        agg[(scale, max_edge)] = total_counts

    # side-by-side suspect term comparison
    print(f"\n{'='*80}\n{'SUSPECT TERM COUNTS (correct vs error)':^80}\n{'='*80}", flush=True)
    header = "  {:<16}".format("term")
    for scale, max_edge in configs:
        header += f"{scale}x/{max_edge}".rjust(14)
    print(header, flush=True)
    for correct, variants in SUSPECT.items():
        print(f"  [{correct}]", flush=True)
        for v in variants:
            mark = " (✓correct)" if v == correct else " (✗error)" if v in ("阀阀", "橡胶脚", "橡胶海", "橡胶辨", "倒流防上器", "例流", "消音") else ""
            row = f"    {v:<14}"
            for cfg in configs:
                row += str(agg[cfg].get(v, 0)).rjust(14)
            print(row + mark, flush=True)
    print(flush=True)
    print(f"HTML saved under {out_dir.relative_to(ROOT)}/ for manual diff.", flush=True)


if __name__ == "__main__":
    main()
