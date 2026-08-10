"""vl_structured_e2e.py - experimental VL->JSON E2E for quote PDFs.

This script is deliberately side-effect free:
  - does not touch DB
  - does not update official OCR snapshots/fixtures
  - writes diagnostics under tmp/vl_structured_e2e_<doc>_<timestamp>/

It reuses the current visual page classifier and compares a direct
visual-language extraction path against the same Excel golden files used by
scripts/fresh_e2e.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.fresh_e2e import (  # noqa: E402
    DOC_CONFIGS,
    _compute_total_incl,
    _diff_vs_golden,
    _draft_to_dict,
    _load_golden,
)


VL_QUOTE_PROMPT = """你是机电材料投标报价清单解析助手。请直接阅读这张页面图片，逐行提取报价明细，返回严格 JSON。

任务边界：
- 只提取材料/设备报价明细行，不要表头、章节标题、小计、合计、页脚、说明文字。
- 若页面不是报价明细表，返回 {"items": []}。
- 页面可能横向或旋转，请自行按表格真实方向阅读。
- 表格可能是续页，没有表头；请结合列位置、数值关系和上下文判断列义。

字段要求：
- seq: 序号，若页面无序号列则留空字符串。
- material: 材料名称，按原文，不要改写。
- spec: 规格型号中表示 DN/尺寸/压力等规格的文本。
- model: 独立型号列；没有则空字符串。
- brand: 品牌；没有则空字符串。
- unit: 单位。
- qty: 数量，数字；无法确定则 null。
- unit_price: 仅当表头没有含税/不含税口径时填写；否则 null。
- unit_price_excl_tax / total_price_excl_tax: 不含税单价/合价。
- unit_price_incl_tax / total_price_incl_tax: 含税单价/合价。
- tax_rate: 税率小数，如 13% 返回 0.13。
- tax_amount: 税额。
- material_type: 材质；没有则空字符串。
- remark: 系统/专业/备注。
- canonical: 阀门类可填 {valve_type,dn,pn,material,connection}，不确定则 {}。
- normalized_material / ocr_correction_reason: 只有明显 OCR 错别字且确信时填写，否则空字符串。
- row_type: 正常报价行填 quote_line；非商品行如小计/合计/说明若误入，请填 invalid/subtotal/grand_total，不要填 quote_line。

价格规则：
- 含税/不含税字段必须来自表格原始列，不要自行用 1.13 推导生成缺失字段。
- tax_amount 是税额，不是含税单价或含税合价。
- 若同时有不含税合价、税额、含税合价，三者关系应满足：不含税合价 + 税额 ≈ 含税合价。

只返回 JSON，不要解释，不要 markdown：
{"supplier_name":"","items":[{"row_type":"quote_line","seq":"","material":"","spec":"","model":"","brand":"","unit":"","qty":null,"unit_price":null,"unit_price_excl_tax":null,"unit_price_incl_tax":null,"total_price":null,"total_price_excl_tax":null,"total_price_incl_tax":null,"tax_rate":null,"tax_amount":null,"material_type":"","remark":"","canonical":{},"normalized_material":"","ocr_correction_reason":""}]}
"""


def _serialise_rows(rows) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "row_type": r.row_type,
            "page": r.source_ref.page if r.source_ref else None,
            "row": r.source_ref.row if r.source_ref else None,
            "seq": r.fields.get("seq"),
            "name": r.fields.get("name"),
            "spec": r.fields.get("spec"),
            "qty": r.fields.get("qty"),
            "unit_price_incl_tax": r.fields.get("unit_price_incl_tax"),
            "total_price_incl_tax": r.fields.get("total_price_incl_tax") or r.fields.get("total_price"),
            "validation_flags": r.validation_flags,
        })
    return out


def run_one(
    doc_name: str,
    out_dir: Path,
    *,
    model: str | None = None,
    max_pixels: int = 8_000_000,
    pages: set[int] | None = None,
    dry_run: bool = False,
) -> dict:
    if doc_name not in DOC_CONFIGS:
        raise SystemExit(f"Unknown doc {doc_name!r}; valid={list(DOC_CONFIGS)}")
    cfg = DOC_CONFIGS[doc_name]
    pdf_path = Path(cfg["pdf"])
    golden_rows = _load_golden(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        return {
            "doc": doc_name,
            "dry_run": True,
            "pdf": str(pdf_path),
            "golden_rows": len(golden_rows),
            "row_count": None,
            "expected": cfg["expected"],
            "pass": True,
        }

    from apps.api.core.config import get_settings
    from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
    from apps.api.intelligence.extraction_draft import ExtractionDraft, PageMetric, compute_quality
    from apps.api.intelligence.page_classifier import QUOTE_TARGET_ROLES
    from apps.api.intelligence.pipeline import _get_quote_adapter
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider
    from apps.api.intelligence.table_recognizer import (
        _classify_pages,
        _dedup_cross_page,
        _infer_missing_seqs,
        _raw_items_to_draft_rows,
        _rotate_png_bytes,
        _validate_arithmetic,
    )

    settings = get_settings()
    provider = DashScopeOCRProvider(
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.DASHSCOPE_BASE_URL,
        ocr_model=settings.DASHSCOPE_OCR_MODEL,
        llm_model=settings.DASHSCOPE_LLM_MODEL,
    )
    adapter = _get_quote_adapter()

    t0 = time.time()
    thumbnails = DocumentLoader.to_thumbnails(str(pdf_path), max_pages=MAX_PAGES_UNLIMITED)
    page_cls, flash_pages, plus_pages = _classify_pages(
        provider, thumbnails, "quote", file_path=str(pdf_path),
        render_full=lambda p: DocumentLoader.render_pages(str(pdf_path), [p])[p],
    )
    target_pages = sorted(c.page for c in page_cls if c.role in QUOTE_TARGET_ROLES)
    if pages:
        target_pages = [p for p in target_pages if p in pages]
    rendered = DocumentLoader.render_pages(str(pdf_path), target_pages)

    rows = []
    metrics = []
    page_debug = []
    vl_calls = 0
    for c in page_cls:
        if c.page not in target_pages:
            continue
        img = rendered[c.page]
        if c.orientation:
            img = _rotate_png_bytes(img, c.orientation)
        p0 = time.time()
        try:
            data, raw, _tok = provider._extract_structured_experimental(
                img, VL_QUOTE_PROMPT, model=model, max_pixels=max_pixels
            )
            vl_calls += 1
            items = data.get("items") or []
            page_rows = _raw_items_to_draft_rows(items, c.page, None, adapter.name_key)
            for r in page_rows:
                r.fields["parser_mode"] = "vl_structured"
            rows.extend(page_rows)
            qlines = sum(1 for r in page_rows if r.row_type == "quote_line")
            metrics.append(PageMetric(
                page=c.page, page_index=c.page - 1, role=str(c.role.value),
                input_mode="vl_structured", extracted_rows=qlines,
                rotation_applied=c.orientation or 0,
            ))
            page_debug.append({
                "page": c.page,
                "role": c.role.value,
                "orientation": c.orientation,
                "items": len(items),
                "quote_lines": qlines,
                "duration_s": round(time.time() - p0, 2),
                "raw_preview": str(raw)[:500],
            })
        except Exception as exc:
            metrics.append(PageMetric(
                page=c.page, page_index=c.page - 1, role=str(c.role.value),
                input_mode="vl_structured", fallback_reason=str(exc),
                extracted_rows=0,
                rotation_applied=c.orientation or 0,
            ))
            page_debug.append({
                "page": c.page,
                "role": c.role.value,
                "orientation": c.orientation,
                "error": str(exc),
                "duration_s": round(time.time() - p0, 2),
            })

    rows = _dedup_cross_page(rows, adapter.name_key)
    rows = _infer_missing_seqs(rows)
    rows = _validate_arithmetic(rows)
    quality = compute_quality(
        rows=rows,
        page_metrics=metrics,
        total_pages=DocumentLoader.get_page_count(str(pdf_path)),
        target_pages=target_pages,
        declared_total=cfg.get("declared"),
        rendered_pages=len(thumbnails),
        ocr_success_pages=0,
        ocr_failed_pages=0,
        ocr_failed_indices=[],
    )
    draft = ExtractionDraft(
        doc_type="quote",
        source_file=str(pdf_path),
        page_count=quality.total_pages,
        processed_page_count=len(target_pages),
        target_pages=target_pages,
        rows=rows,
        meta={"declared_total": cfg.get("declared"), "experiment": "vl_structured"},
        quality=quality,
    )

    diff = _diff_vs_golden(draft.rows, golden_rows, cfg)
    total = _compute_total_incl(draft.rows)
    result = {
        "doc": doc_name,
        "model": model or "default",
        "max_pixels": max_pixels,
        "elapsed_s": round(time.time() - t0, 1),
        "target_pages": target_pages,
        "flash_pages": flash_pages,
        "plus_pages": plus_pages,
        "vl_calls": vl_calls,
        "quality": draft.quality.to_dict(),
        "row_count": diff["total_extracted"],
        "expected": cfg["expected"],
        "matched": len(diff["matched"]),
        "missing": diff["missing_seqs"],
        "extra": diff["extra_seqs"],
        "no_seq_rows": diff["no_seq_extracted"],
        "total_incl_ext": total,
        "total_incl_declared": cfg.get("declared"),
        "total_gap": total - float(cfg.get("declared") or 0),
        "pass": (
            diff["total_extracted"] == cfg["expected"]
            and not diff["missing_seqs"]
            and not diff["extra_seqs"]
            and abs(total - float(cfg.get("declared") or 0)) / max(float(cfg.get("declared") or 1), 1) < 0.001
        ),
    }

    (out_dir / "draft.json").write_text(json.dumps(_draft_to_dict(draft), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "per_row.json").write_text(json.dumps(_serialise_rows(draft.rows), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "page_debug.json").write_text(json.dumps(page_debug, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "golden_diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Experimental VL direct structured E2E")
    parser.add_argument("docs", nargs="*", default=["kaishuo"], help="kaishuo taikelong miancun all")
    parser.add_argument("--model", default=None, help="DashScope VL model, e.g. qwen3-vl-plus")
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=8_000_000,
        help="DashScope image max_pixels for VL structured calls",
    )
    parser.add_argument(
        "--render-scale",
        type=float,
        default=None,
        help="Override OCR_RENDER_SCALE before rendering PDF pages",
    )
    parser.add_argument(
        "--max-edge-px",
        type=int,
        default=None,
        help="Override OCR_MAX_EDGE_PX before rendering PDF pages",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="Comma-separated 1-based pages to run after visual classification, for focused A/B tests",
    )
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.render_scale is not None:
        os.environ["OCR_RENDER_SCALE"] = str(args.render_scale)
    if args.max_edge_px is not None:
        os.environ["OCR_MAX_EDGE_PX"] = str(args.max_edge_px)
    pages = None
    if args.pages:
        pages = {int(p.strip()) for p in args.pages.split(",") if p.strip()}

    docs = []
    for d in args.docs:
        if d == "all":
            docs.extend(DOC_CONFIGS)
        else:
            docs.append(d)
    docs = list(dict.fromkeys(docs))
    base = Path(args.out) if args.out else REPO / "tmp"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    results = []
    for doc in docs:
        out_dir = base / f"vl_structured_e2e_{doc}_{ts}"
        print(f"\n[VL] {doc} -> {out_dir}")
        result = run_one(
            doc,
            out_dir,
            model=args.model,
            max_pixels=args.max_pixels,
            pages=pages,
            dry_run=args.dry_run,
        )
        results.append(result)
        print(json.dumps({
            "doc": result.get("doc"),
            "pass": result.get("pass"),
            "rows": f"{result.get('row_count')}/{result.get('expected')}",
            "matched": result.get("matched"),
            "missing": result.get("missing"),
            "extra_count": len(result.get("extra") or []),
            "total_gap": result.get("total_gap"),
            "elapsed_s": result.get("elapsed_s"),
            "vl_calls": result.get("vl_calls"),
        }, ensure_ascii=False, indent=2))

    (base / f"vl_structured_e2e_summary_{ts}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    raise SystemExit(0 if all(r.get("pass") for r in results) else 1)


if __name__ == "__main__":
    main()
