"""run_baseline.py — 阶段二 baseline：4 份文档确定性重放 + 严格 diff。

流程：
  for each doc:
    快照存在 → SnapshotProvider(inner=None, replay)  ← 纯快照，不打真实 API
    快照不存在 → SnapshotProvider(inner=real, record) ← 打 API 并落快照
    → recognize_tables → ExtractionDraft.rows vs golden → diff_doc → outputs/e2e_diff/<doc>/

Golden 来源（按优先级）：
  data/golden/<doc>.json  ← 版本化、含 field_sources 和 SHA256，优先使用
  不再从 Excel 即时重建，golden JSON 必须存在

用法：
    python scripts/run_baseline.py [doc_name ...]      # 省略则跑全部
    python scripts/run_baseline.py --record [doc_name ...]  # 强制重新打 API 更新快照
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.e2e_diff import diff_doc, write_outputs  # noqa: E402

DOCS = REPO / "docs" / "test"
GOLDEN_DIR = REPO / "data" / "golden"
# Controlled fixture snapshots (version-controlled); outputs/ocr_snapshots is transient
SNAP_DIR = REPO / "tests" / "fixtures" / "ocr_snapshots"

# doc_name → {pdf, golden_json, doc_type}
BASELINE = {
    "quote_taikelong": {
        "pdf": DOCS / "泰科龙投标文件.pdf",
        "golden": GOLDEN_DIR / "quote_taikelong.json",
    },
    "quote_miancun": {
        "pdf": DOCS / "上海绵存投标文件.pdf",
        "golden": GOLDEN_DIR / "quote_miancun.json",
    },
    "quote_kaishuo": {
        "pdf": DOCS / "凯硕新正投标文件.pdf",
        "golden": GOLDEN_DIR / "quote_kaishuo.json",
    },
    "tender_jingqiao": {
        "pdf": DOCS / "金桥地体上盖招标文件.pdf",
        "golden": GOLDEN_DIR / "tender_jingqiao.json",
    },
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


def _load_golden(cfg: dict) -> tuple[dict, dict | None]:
    """Load golden from data/golden/<doc>.json. Returns (golden_dict, field_sources)."""
    path = Path(cfg["golden"])
    if not path.exists():
        raise FileNotFoundError(
            f"Golden JSON 不存在: {path}\n"
            "请先运行 scripts/audit_golden.py 并生成 data/golden/<doc>.json"
        )
    g = json.loads(path.read_text(encoding="utf-8"))
    field_sources = g.get("field_sources")
    return g, field_sources


def run_one(doc_name: str, cfg: dict, force_record: bool = False,
            fresh: bool = False) -> dict:
    """Run one document.

    force_record: update snapshot from API (record mode).
    fresh:禁止复用任何快照，每次API全量重新调用（用于 fresh E2E 验收）。
           使用临时快照路径，不覆盖版本控制的 fixture 快照。
    """
    import time
    from apps.api.intelligence.snapshot_provider import SnapshotProvider
    from apps.api.intelligence.table_recognizer import recognize_tables
    from apps.api.intelligence.pipeline import _get_quote_adapter
    from apps.api.services.tender_pdf import TENDER_ADAPTER

    pdf = Path(cfg["pdf"])
    if not pdf.exists():
        return {"doc": doc_name, "error": f"PDF 不存在: {pdf}"}

    golden, field_sources = _load_golden(cfg)

    snap_path = SNAP_DIR / f"{doc_name}.json"

    t_start = time.time()
    if fresh:
        # 禁止复用快照：使用空临时快照，强制全量 API 调用
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(
            suffix=f"_{doc_name}.json", delete=False,
            dir=REPO / "outputs", prefix="fresh_snap_")
        tmp.close()
        fresh_path = Path(tmp.name)
        inner = _build_provider()
        provider = SnapshotProvider(inner, fresh_path, mode="record")
        print(f"[{doc_name}] FRESH 模式（临时快照 {fresh_path.name}，不复用任何缓存）")
    elif snap_path.exists() and not force_record:
        provider = SnapshotProvider(None, snap_path, mode="replay")
        print(f"[{doc_name}] replay 模式（快照: {snap_path.name}）")
    else:
        inner = _build_provider()
        provider = SnapshotProvider(inner, snap_path, mode="record")
        print(f"[{doc_name}] record 模式 → {snap_path.name}")

    # Pick adapter by doc_type
    doc_type = golden.get("doc_type", "quote")
    adapter = TENDER_ADAPTER if doc_type == "tender" else _get_quote_adapter()

    print(f"[{doc_name}] 识别 {pdf.name} ...")
    draft = recognize_tables(str(pdf), provider, adapter)
    if fresh:
        provider.save()
        elapsed = time.time() - t_start
        s = provider.stats
        print(f"[{doc_name}] FRESH 统计: ocr_calls={s['ocr_misses']} "
              f"llm_calls={s['llm_misses']} visual_calls={s['visual_misses']} "
              f"耗时={elapsed:.1f}s")
    elif not snap_path.exists() or force_record:
        provider.save()
    print(f"[{doc_name}] 快照统计: {provider.stats}")

    result = diff_doc(doc_name, golden, draft.rows, field_sources)
    out_dir = write_outputs(doc_name, result)

    # Attach quality gate state to summary
    q = draft.quality
    result["summary"]["pipeline"] = {
        "quality_status": q.status,
        "blocking_reasons": q.blocking_reasons,
        "total_pages": q.total_pages,
        "rendered_pages": q.rendered_pages,
        "ocr_success_pages": q.ocr_success_pages,
        "ocr_failed_pages": q.ocr_failed_pages,
        "ocr_failed_indices": q.ocr_failed_indices,
        "processed_pages": q.processed_pages,
        "truncated": q.truncated,
        "source_ref_coverage": q.source_ref_coverage,
        "bbox_coverage": q.bbox_coverage,
        "subtotal_count": q.subtotal_count,
        "grand_total_count": q.grand_total_count,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")

    rl = result["summary"]["row_level"]
    mode_tag = f" [{rl.get('match_mode', 'seq')}]" if rl.get("match_mode") else ""
    print(f"[{doc_name}] recall={rl['row_recall']:.0%} precision={rl['row_precision']:.0%} "
          f"matched={rl['matched']}/{rl['golden_rows']} missing={len(rl['missing'])} "
          f"extra={len(rl['extra'])} no_seq={rl['no_seq_rows']} quality={q.status}{mode_tag}")
    return result["summary"]


def main():
    SNAP_DIR.mkdir(parents=True, exist_ok=True)

    args = sys.argv[1:]
    force_record = "--record" in args
    fresh = "--fresh" in args
    args = [a for a in args if a not in ("--record", "--fresh")]
    targets = args or list(BASELINE)

    if fresh:
        print("=== FRESH E2E 模式：禁止复用 visual/OCR/LLM 快照，全量重新调用 API ===")

    all_summaries = []
    for name in targets:
        if name not in BASELINE:
            print(f"[skip] 未知文档: {name}")
            continue
        try:
            s = run_one(name, BASELINE[name], force_record=force_record, fresh=fresh)
            all_summaries.append(s)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[ERR] {name}: {exc}")

    out = REPO / "outputs" / "e2e_diff" / "_baseline_summary.json"
    out.write_text(json.dumps(all_summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ baseline 汇总: {out.relative_to(REPO)}")


if __name__ == "__main__":
    main()
