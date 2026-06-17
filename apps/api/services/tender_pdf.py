"""招标文件 PDF → 投标清单锚点 + 品牌映射抽取。

复用现有 OCR 层（DocumentLoader + DashScopeOCRProvider.ocr_pages_with_roles），
只新增「页范围自动定位」+ 两个新 prompt（投标清单 / 招标情况表）的桥接。

为什么走 OCR：该类招标 PDF 文本层是乱码（自定义 CMap，pypdf 抽出 mojibake），
必须渲染成图片再 OCR。pypdfium2 可正常打开带权限标志位的 PDF。

输出 dict（兼容 ExtractionJob.result）:
{
  "items": [anchor_json...],          # TenderAnchor 序列化，含 category/canonical/source_ref
  "brand_requirement": [...],
  "supplier_brands": [...],
  "material_class": "水阀门",
  "detected_pages": {"bidlist": [...], "brand": int|None},
  "row_count": int,
  "source_type": "pdf",
  "page_diagnostics": [...],          # 逐页：input_mode/fallback_reason/expected_rows/extracted_rows
  "quality_metrics": {...},           # 字段级覆盖率：材质/品牌/seq gap/数量解析成功率 等
  "reconcile": {...} | None,          # 仅当调用时传入 xlsx_path 才有值
}
"""

from __future__ import annotations

import json
import logging
import re

from apps.api.intelligence.document_loader import DocumentLoader, MAX_PAGES_UNLIMITED
from apps.api.intelligence.prompts import TENDER_BIDLIST_PROMPT, TENDER_BRANDTABLE_PROMPT
from apps.api.services.tender_list import TenderAnchor, anchor_to_json
from apps.api.services.canonical import extract_valve_canonical

log = logging.getLogger(__name__)

ProgressCallback = "Callable[[str, int], None]"

_DN_RE = re.compile(r"DN\s*\d", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_OUTER_RE = re.compile(r"</?(?:html|head|body)[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]{2,}")


# ─── HTML 预处理 ─────────────────────────────────────────────────────────────

def _strip_html_noise(html: str) -> str:
    """Strip CSS/script/outer-boilerplate from OCR HTML, preserve raw table markup."""
    html = _STYLE_RE.sub("", html)
    html = _SCRIPT_RE.sub("", html)
    html = _OUTER_RE.sub("", html)
    html = _WS_RE.sub(" ", html)
    return html.strip()


# ─── 页范围评分（多信号，非关键词二值）────────────────────────────────────────

def _score_page(html: str) -> tuple[float, float]:
    """Score a page's OCR HTML for bidlist vs brand-table likelihood.

    Returns (bidlist_score, brand_score) each in [0.0, 1.0].

    Decision thresholds:
      brand   → br >= 0.5 AND br > bs
      bidlist → bs >= 0.35 AND NOT classified as brand
    """
    if not html:
        return 0.0, 0.0

    bs = 0.0
    if "投标清单" in html:                                          bs += 0.5
    if "<table" in html:                                            bs += 0.2
    if "序号" in html and ("项目名称" in html or "名称" in html):   bs += 0.2
    if "工作压力" in html:                                          bs += 0.3
    if any(kw in html for kw in ("阀体", "密封圈", "阀芯")):        bs += 0.2
    if _DN_RE.search(html):                                         bs += 0.2
    if "给排水" in html:                                            bs += 0.1
    bs = min(bs, 1.0)

    br = 0.0
    if "招标情况表" in html:                                         br += 0.8
    if "参与品牌" in html or "业主招标品牌" in html:                 br += 0.5
    if "投标单位" in html and "品牌" in html:                        br += 0.3
    br = min(br, 1.0)

    return bs, br


def _is_brand_page(html: str) -> bool:
    bs, br = _score_page(html)
    return br >= 0.5 and br > bs


def _is_bidlist_page(html: str) -> bool:
    bs, br = _score_page(html)
    return bs >= 0.35 and not (br >= 0.5 and br > bs)


def _detect_pages(
    page_htmls: list[str],
) -> tuple[list[int], int | None]:
    """Returns (bidlist_pages_1based, brand_page_1based) using multi-signal scoring."""
    brand_page: int | None = None
    bidlist: list[int] = []
    for i, html in enumerate(page_htmls):
        page_no = i + 1
        bs, br = _score_page(html)
        is_brand = br >= 0.5 and br > bs
        is_bidlist = bs >= 0.35 and not is_brand
        if is_brand:
            if brand_page is None:
                brand_page = page_no
            log.debug("Page %d → brand   (br=%.2f bs=%.2f)", page_no, br, bs)
        elif is_bidlist:
            bidlist.append(page_no)
            log.debug("Page %d → bidlist (bs=%.2f br=%.2f)", page_no, bs, br)
        else:
            log.debug("Page %d → skip    (bs=%.2f br=%.2f)", page_no, bs, br)
    return bidlist, brand_page


# ─── LLM 输入构建（TableGrid 中间件 + 降噪）──────────────────────────────────

def _build_llm_input(html: str, page_no: int) -> tuple[str, int, str, str]:
    """Prepare LLM input string and estimate expected row count.

    Returns:
        (llm_input_str, expected_rows, input_mode, fallback_reason)

    input_mode values: "table_grid" | "html_fallback"
    fallback_reason values: "" | "duplicate_headers" | "no_grids"

    Strategy:
    1. Parse HTML into TableGrid structures (rowspan/colspan expansion).
    2. If all header columns within each grid are unique (no nested-header
       collision), emit compact structured JSON — smaller token footprint.
    3. Otherwise (nested material sub-columns → duplicate "材质" header keys),
       fall back to noise-stripped raw HTML so the LLM sees the full structure.
    4. expected_rows from quote_line_count() drives the thinking-retry gate.
    """
    from apps.api.intelligence.table_parser import html_to_table_grids

    grids = html_to_table_grids(html, page_num=page_no)
    if grids:
        all_unique_headers = all(len(set(g.header)) == len(g.header) for g in grids)
        expected_rows = sum(g.quote_line_count() for g in grids)

        if all_unique_headers:
            rows_out = []
            for g in grids:
                rows = [
                    {"row_index": r.row_index, "row_type": r.row_type, "cells": r.cells}
                    for r in g.rows
                    if r.row_type not in ("empty", "grand_total", "subtotal")
                ]
                if rows:
                    rows_out.append({"page": g.page, "table_index": g.table_index, "rows": rows})
            return (
                json.dumps(rows_out, ensure_ascii=False, separators=(",", ":")),
                expected_rows,
                "table_grid",
                "",
            )
        else:
            log.debug("Page %d: duplicate headers (nested table) — using stripped HTML", page_no)
            return _strip_html_noise(html), expected_rows, "html_fallback", "duplicate_headers"

    return _strip_html_noise(html), 0, "html_fallback", "no_grids"


# ─── 字段级质量指标 ───────────────────────────────────────────────────────────

def _compute_quality_metrics(
    items: list[dict],
    page_diagnostics: list[dict],
) -> dict:
    """Compute field-level quality indicators from extracted anchor items.

    Returns:
        seq_missing            : numeric seq gaps in the extracted range
        seq_duplicate          : seqs that appear more than once
        material_columns_filled_rate : fraction of items with any material sub-column filled
        brand_filled_rate      : fraction of items with non-empty brand
        source_ref_coverage    : fraction of items with source_ref populated
        qty_parse_success_rate : fraction of items with parseable qty (not null)
        row_count_by_page      : {page: count} breakdown
        table_grid_pages       : pages that used compact TableGrid JSON input
        html_fallback_pages    : pages that fell back to raw HTML (with reason)
    """
    n = len(items)
    zero: dict = {
        "seq_missing": [], "seq_duplicate": [],
        "material_columns_filled_rate": 0.0,
        "brand_filled_rate": 0.0,
        "source_ref_coverage": 0.0,
        "qty_parse_success_rate": 0.0,
        "row_count_by_page": {},
        "table_grid_pages": [],
        "html_fallback_pages": [],
    }
    if n == 0:
        return zero

    # seq analysis
    seqs = [str(it.get("seq", "")).strip() for it in items if it.get("seq") is not None]
    seq_counts: dict[str, int] = {}
    for s in seqs:
        seq_counts[s] = seq_counts.get(s, 0) + 1
    seq_duplicate = sorted(s for s, c in seq_counts.items() if c > 1)

    numeric_seqs = sorted(int(s) for s in seqs if s.isdigit())
    seq_missing: list[str] = []
    if numeric_seqs:
        full_range = set(range(numeric_seqs[0], numeric_seqs[-1] + 1))
        seq_missing = sorted(str(s) for s in (full_range - set(numeric_seqs)))

    # field coverage
    mat_filled    = sum(1 for it in items if any((it.get("materials") or {}).values()))
    brand_filled  = sum(1 for it in items if str(it.get("brand") or "").strip())
    src_ref_ok    = sum(1 for it in items if it.get("source_ref"))
    qty_ok        = sum(1 for it in items if it.get("qty") is not None)

    # row count by page
    row_count_by_page: dict[str, int] = {}
    for it in items:
        page = str((it.get("source_ref") or {}).get("page", "?"))
        row_count_by_page[page] = row_count_by_page.get(page, 0) + 1

    # page-level input mode summary
    tg_pages = [d["page"] for d in page_diagnostics if d["input_mode"] == "table_grid"]
    fb_pages = [
        {"page": d["page"], "reason": d["fallback_reason"]}
        for d in page_diagnostics
        if d["input_mode"] == "html_fallback"
    ]

    return {
        "seq_missing": seq_missing,
        "seq_duplicate": seq_duplicate,
        "material_columns_filled_rate": round(mat_filled / n, 3),
        "brand_filled_rate": round(brand_filled / n, 3),
        "source_ref_coverage": round(src_ref_ok / n, 3),
        "qty_parse_success_rate": round(qty_ok / n, 3),
        "row_count_by_page": row_count_by_page,
        "table_grid_pages": tg_pages,
        "html_fallback_pages": fb_pages,
    }


# ─── 锚点构建 ────────────────────────────────────────────────────────────────

def _row_to_anchor(row: dict, page_no: int) -> TenderAnchor | None:
    """单行 LLM 输出 → TenderAnchor（含 canonical + source_ref）。"""
    name = str(row.get("name") or "").strip()
    seq = row.get("seq")
    if not name or seq in (None, ""):
        return None
    materials = row.get("materials") or {}
    # 仅保留有值的子列，键固定五项
    materials = {
        k: str(v).strip()
        for k, v in materials.items()
        if isinstance(v, str) and v.strip()
    }
    qty_raw = row.get("qty")
    try:
        qty = float(qty_raw) if qty_raw not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    anchor = TenderAnchor(
        seq=seq,
        name=name,
        spec=str(row.get("spec") or "").strip(),
        model=str(row.get("model") or "").strip(),
        pressure=str(row.get("pressure") or "").strip(),
        materials=materials,
        unit=str(row.get("unit") or "").strip(),
        qty=qty,
        brand=str(row.get("brand") or "").strip(),
        profession=str(row.get("profession") or "").strip(),
        remark=str(row.get("remark") or "").strip(),
        source_ref={"page": page_no, "row": seq},
    )
    anchor.canonical = extract_valve_canonical(
        anchor.name, anchor.spec, anchor.pressure, anchor.material_text()
    )
    return anchor


# ─── 主入口 ─────────────────────────────────────────────────────────────────

def extract_bidlist(
    file_path: str,
    provider,
    progress_cb=None,
    bidlist_pages: list[int] | None = None,
    brand_page: int | None = None,
    default_category: str = "阀门",
    xlsx_path: str | None = None,
) -> dict:
    """从招标文件 PDF 抽取投标清单锚点 + 品牌映射。

    Args:
        file_path: PDF 路径。
        provider: DashScopeOCRProvider（需有 ocr_pages_with_roles + _llm_call_json）。
        bidlist_pages / brand_page: 1-based 手动指定页（用户修正用）；None 则自动定位。
        default_category: 强制品类（本类招标文件单品类，默认阀门）。
        xlsx_path: 可选 Excel 清单路径；传入时自动运行 source reconciliation。
    """
    if not (hasattr(provider, "ocr_pages_with_roles") and hasattr(provider, "_llm_call_json")):
        raise RuntimeError(
            "tender_pdf.extract_bidlist 需要 DashScopeOCRProvider（OCR 能力）；"
            "当前 provider 不支持。请配置 DASHSCOPE_API_KEY。"
        )

    def _notify(stage: str, pct: int) -> None:
        if progress_cb:
            progress_cb(stage, pct)

    _notify("渲染PDF", 10)
    images = DocumentLoader.to_images(file_path, max_pages=MAX_PAGES_UNLIMITED)

    _notify("OCR识别全文", 20)
    page_roles_html, _failures = provider.ocr_pages_with_roles(images)
    page_htmls = [html for (_cls, html) in page_roles_html]

    # 自动定位（除非手动指定）
    auto_bidlist, auto_brand = _detect_pages(page_htmls)
    if not bidlist_pages:
        bidlist_pages = auto_bidlist
    if brand_page is None:
        brand_page = auto_brand

    if not bidlist_pages:
        raise ValueError(
            "未能在 PDF 中定位「投标清单」页（含 序号/项目名称/工作压力/材质）。"
            "请确认上传的是招标文件，或手动指定清单页范围。"
        )

    log.info("tender_pdf: detected bidlist pages=%s brand_page=%s", bidlist_pages, brand_page)

    # ── 逐页抽取清单 ──────────────────────────────────────────────────────
    anchors: list[TenderAnchor] = []
    page_diagnostics: list[dict] = []
    n_pages = len(bidlist_pages)

    for idx, page_no in enumerate(bidlist_pages):
        html = page_htmls[page_no - 1]
        if not html.strip():
            page_diagnostics.append({
                "page": page_no, "input_mode": "html_fallback",
                "fallback_reason": "empty_html",
                "expected_rows": 0, "extracted_rows": 0,
                "thinking_retry": False,
            })
            continue
        _notify(f"解析清单 第{page_no}页", 30 + int(50 * idx / max(1, n_pages)))

        llm_input, expected_rows, input_mode, fallback_reason = _build_llm_input(html, page_no)
        data, _raw, _tok = provider._llm_call_json(TENDER_BIDLIST_PROMPT, llm_input)
        items = data.get("items") or []
        thinking_retry = False

        if expected_rows > 0 and len(items) < expected_rows * 0.7:
            log.warning(
                "Page %d: LLM returned %d items, expected ~%d — retrying with thinking",
                page_no, len(items), expected_rows,
            )
            data, _raw, _tok = provider._llm_call_json(
                TENDER_BIDLIST_PROMPT, llm_input, enable_thinking=True
            )
            items = data.get("items") or []
            thinking_retry = True

        page_anchors_before = len(anchors)
        for row in items:
            anchor = _row_to_anchor(row, page_no)
            if anchor is not None:
                anchors.append(anchor)

        page_diagnostics.append({
            "page": page_no,
            "input_mode": input_mode,
            "fallback_reason": fallback_reason,
            "expected_rows": expected_rows,
            "extracted_rows": len(anchors) - page_anchors_before,
            "thinking_retry": thinking_retry,
        })

    # ── 品牌表抽取（enable_thinking: 多实体关系，LLM 收益明显）────────────
    brand_requirement: list[dict] = []
    supplier_brands: list[dict] = []
    material_class = ""
    if brand_page is not None:
        _notify("解析招标情况表", 88)
        bhtml = page_htmls[brand_page - 1]
        if bhtml.strip():
            bdata, _raw, _tok = provider._llm_call_json(
                TENDER_BRANDTABLE_PROMPT, bhtml, enable_thinking=True
            )
            brand_requirement = bdata.get("brand_requirement") or []
            supplier_brands = bdata.get("supplier_brands") or []
            material_class = str(bdata.get("material_class") or "")

    _notify("整理结果", 95)
    items_out = [anchor_to_json(a, default_category) for a in anchors]
    quality_metrics = _compute_quality_metrics(items_out, page_diagnostics)

    # ── 可选：与 Excel 对账 ─────────────────────────────────────────────
    reconcile_result: dict | None = None
    if xlsx_path:
        try:
            from apps.api.services.tender_list import parse_tender_xlsx
            from apps.api.services.source_reconcile import reconcile_anchors
            xlsx_anchors = parse_tender_xlsx(xlsx_path)
            xlsx_items = [
                {
                    "seq": str(a.seq),
                    "name": a.name,
                    "spec": a.spec,
                    "unit": a.unit,
                    "qty": a.qty,
                }
                for a in xlsx_anchors
            ]
            reconcile_result = reconcile_anchors(xlsx_items, items_out, source_type="pdf_primary")
            if reconcile_result["recommended_source"] != "both_consistent":
                log.warning(
                    "tender_pdf: reconcile mismatch — missing_in_pdf=%d missing_in_xlsx=%d "
                    "field_mismatches=%d",
                    len(reconcile_result["seq_missing_in_pdf"]),
                    len(reconcile_result["seq_missing_in_xlsx"]),
                    len(reconcile_result["field_mismatches"]),
                )
        except Exception as exc:
            log.error("tender_pdf: reconcile failed: %s", exc)
            reconcile_result = {"error": str(exc)}

    return {
        "items": items_out,
        "brand_requirement": brand_requirement,
        "supplier_brands": supplier_brands,
        "material_class": material_class,
        "detected_pages": {"bidlist": bidlist_pages, "brand": brand_page},
        "row_count": len(items_out),
        "source_type": "pdf_primary",
        "page_diagnostics": page_diagnostics,
        "quality_metrics": quality_metrics,
        "reconcile": reconcile_result,
    }
