"""visual_stability_test.py — 视觉分类稳定性实验（仅跑 Flash，不跑 OCR/LLM）。

对指定文档重复运行 N 次 Flash 页面分类（temperature=0，v4 prompt），
统计每页角色一致率，输出不一致页列表。

用途：在修改 batch 协议后快速验证稳定性，避免等完整 E2E（10+ 分钟）。

用法：
    python scripts/visual_stability_test.py jingqiao [--runs 3] [--doc-type tender]
    python scripts/visual_stability_test.py taikelong [--runs 3] [--doc-type quote]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DOCS = REPO / "docs" / "test"
PDF_MAP = {
    "taikelong": (DOCS / "泰科龙投标文件.pdf", "quote"),
    "miancun":   (DOCS / "上海绵存投标文件.pdf", "quote"),
    "kaishuo":   (DOCS / "凯硕新正投标文件.pdf", "quote"),
    "jingqiao":  (DOCS / "金桥地体上盖招标文件.pdf", "tender"),
}


def _build_provider():
    from apps.api.core.config import get_settings
    s = get_settings()
    if not s.DASHSCOPE_API_KEY:
        raise SystemExit("DASHSCOPE_API_KEY 未配置")
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    return DashScopeOCRProvider(
        api_key=s.DASHSCOPE_API_KEY, base_url=s.DASHSCOPE_BASE_URL,
        ocr_model=s.DASHSCOPE_OCR_MODEL, llm_model=s.DASHSCOPE_LLM_MODEL,
    )


def render_thumbnails(pdf_path: Path) -> list[bytes]:
    from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
    return DocumentLoader.to_thumbnails(str(pdf_path), max_pages=MAX_PAGES_UNLIMITED)


def run_flash_once(provider, thumbnails: list[bytes], doc_type: str) -> list[dict]:
    from apps.api.intelligence.providers.dashscope_ocr import (
        _VISUAL_FLASH_MODEL, _VISUAL_PROMPT_VERSION,
    )
    pages, _failures = provider.classify_pages_visual(
        thumbnails, doc_type,
        model=_VISUAL_FLASH_MODEL,
        prompt_version=_VISUAL_PROMPT_VERSION,
    )
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc", choices=list(PDF_MAP))
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    pdf, doc_type = PDF_MAP[args.doc]
    n_runs = args.runs

    print(f"=== 视觉分类稳定性实验: {args.doc} ({doc_type}), {n_runs} 次 ===")
    print(f"    PDF: {pdf.name}")

    provider = _build_provider()
    print("渲染缩略图...", end=" ", flush=True)
    thumbs = render_thumbnails(pdf)
    print(f"{len(thumbs)} 页")

    all_runs: list[dict[int, str]] = []  # run_i → {page → role}
    for run_i in range(n_runs):
        t0 = time.time()
        pages = run_flash_once(provider, thumbs, doc_type)
        elapsed = time.time() - t0
        run_roles = {p["page"]: p["role"] for p in pages}
        all_runs.append(run_roles)
        tgt = [r for r in run_roles.values() if "table" in r]
        print(f"  Run {run_i + 1}: {elapsed:.1f}s  目标页数={len(tgt)}  角色分布="
              + str(dict(Counter(run_roles.values()))))

    # Per-page consistency
    all_pages = sorted(set(p for r in all_runs for p in r))
    inconsistent = []
    for p in all_pages:
        roles = [r.get(p, "MISSING") for r in all_runs]
        if len(set(roles)) > 1:
            inconsistent.append((p, roles))

    n_consistent = len(all_pages) - len(inconsistent)
    print(f"\n=== 一致性统计 ===")
    print(f"  总页数: {len(all_pages)}")
    print(f"  一致页: {n_consistent}/{len(all_pages)} = {n_consistent/len(all_pages):.0%}")
    print(f"  不一致页: {len(inconsistent)}")

    if inconsistent:
        print("\n  不一致页详情:")
        for p, roles in inconsistent:
            print(f"    p{p:>3}: {' / '.join(roles)}")
    else:
        print("  所有页面三次结果完全一致。")

    # Also show target page stability
    try:
        golden_path = REPO / "data" / "golden" / f"pages_{args.doc}.json"
        g = json.loads(golden_path.read_text(encoding="utf-8"))
        gold_roles = {p["page"]: p["role"] for p in g["pages"]}
        # Only count extraction target roles (what actually goes to OCR)
        doc_type_local = args.doc.split("_")[0] if "_" in args.doc else doc_type
        if "tender" in g.get("doc_type", doc_type):
            tgt_pattern = "tender_table"
        else:
            tgt_pattern = "quote_table"
        tgt_gold = {p for p, r in gold_roles.items() if tgt_pattern in r}
        print(f"\n=== 抽取目标页召回（{tgt_pattern}_*，对照 golden） ===")
        print(f"  golden 目标页: {sorted(tgt_gold)} (n={len(tgt_gold)})")
        for run_i, run_roles in enumerate(all_runs):
            tgt_pred = {p for p, r in run_roles.items() if tgt_pattern in r}
            hit = tgt_pred & tgt_gold
            fp = tgt_pred - tgt_gold
            fn = tgt_gold - tgt_pred
            print(f"  Run {run_i + 1}: 召回={len(hit)}/{len(tgt_gold)}={len(hit)/max(1,len(tgt_gold)):.0%}  "
                  f"FP={len(fp)} {sorted(fp)}  FN={len(fn)} {sorted(fn)}")
    except Exception as e:
        print(f"\n(golden 对比跳过: {e})")


if __name__ == "__main__":
    main()
