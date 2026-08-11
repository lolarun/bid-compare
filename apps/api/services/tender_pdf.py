"""招标文件 PDF → 投标清单锚点 + 品牌映射抽取。

公共入口 extract_bidlist 签名不变；内部改用 recognize_tables 公共骨架。

输出 dict（兼容 ExtractionJob.result）：
{
  "items": [anchor_json...],          # TenderAnchor 序列化
  "brand_requirement": [...],
  "supplier_brands": [...],
  "material_class": str,
  "detected_pages": {"bidlist": [...], "brand": int|None},
  "row_count": int,
  "source_type": "pdf_primary",
  "page_diagnostics": [...],          # PageMetric 转换
  "quality_metrics": {...},           # 字段级指标（兼容旧格式）
  "reconcile": {...} | None,
}
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from apps.api.intelligence.prompts import TENDER_BIDLIST_PROMPT, TENDER_BRANDTABLE_PROMPT
from apps.api.intelligence.table_recognizer import RecognizeAdapter, recognize_tables
from apps.api.intelligence.extraction_draft import DraftRow
from apps.api.services.tender_list import TenderAnchor, anchor_to_json
from apps.api.services.canonical import extract_valve_canonical

log = logging.getLogger(__name__)

_DN_RE = re.compile(r"DN\s*\d", re.IGNORECASE)


# ─── 页范围评分（多信号，非关键词二值）────────────────────────────────────────

def _score_page(html: str) -> tuple[float, float]:
    """Score a page's OCR HTML for bidlist vs brand-table likelihood.

    Returns (bidlist_score, brand_score) each in [0.0, 1.0].
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


def _tender_detect_pages(htmls: list[str]) -> list[int]:
    """TenderAdapter 用：只返回投标清单页（品牌页由 extract_meta 另处理）。"""
    bidlist: list[int] = []
    for i, html in enumerate(htmls):
        page_no = i + 1
        bs, br = _score_page(html)
        is_brand = br >= 0.5 and br > bs
        is_bidlist = bs >= 0.35 and not is_brand
        if is_bidlist:
            bidlist.append(page_no)
            log.debug("Page %d → bidlist (bs=%.2f br=%.2f)", page_no, bs, br)
        elif is_brand:
            log.debug("Page %d → brand   (br=%.2f)", page_no, br)
        else:
            log.debug("Page %d → skip    (bs=%.2f br=%.2f)", page_no, bs, br)
    return bidlist


def _tender_extract_meta(
    non_target_htmls: list[tuple[int, str]],
    provider,
) -> dict:
    """从非清单页提取品牌表数据。返回 meta dict，含 brand_page。"""
    brand_requirement: list[dict] = []
    supplier_brands: list[dict] = []
    material_class = ""
    brand_page: int | None = None

    for page_no, html in non_target_htmls:
        if _is_brand_page(html) and brand_page is None:
            brand_page = page_no
            if html.strip():
                try:
                    bdata, _raw, _tok = provider._llm_call_json(
                        TENDER_BRANDTABLE_PROMPT, html, enable_thinking=True
                    )
                    brand_requirement = bdata.get("brand_requirement") or []
                    supplier_brands = bdata.get("supplier_brands") or []
                    material_class = str(bdata.get("material_class") or "")
                except Exception as exc:
                    log.warning("tender brand table extract failed: %s", exc)

    return {
        "brand_requirement": brand_requirement,
        "supplier_brands": supplier_brands,
        "material_class": material_class,
        "brand_page": brand_page,
    }


# ─── 向后兼容：保留旧签名供现有测试调用 ─────────────────────────────────────

def _detect_pages(page_htmls: list[str]) -> tuple[list[int], int | None]:
    """旧签名 → (bidlist_pages, brand_page)，测试仍在用。"""
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
        elif is_bidlist:
            bidlist.append(page_no)
    return bidlist, brand_page


def _row_to_anchor(row: dict, page_no: int) -> TenderAnchor | None:
    """旧签名 → TenderAnchor（接受 raw dict），测试仍在用。"""
    name = str(row.get("name") or "").strip()
    seq = row.get("seq")
    if not name or seq in (None, ""):
        return None
    materials = row.get("materials") or {}
    materials = {
        k: str(v).strip()
        for k, v in materials.items()
        if isinstance(v, str) and v.strip()
    }
    try:
        qty = float(row.get("qty")) if row.get("qty") not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    anchor = TenderAnchor(
        seq=seq, name=name,
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


TENDER_ADAPTER = RecognizeAdapter(
    doc_type="tender",
    detect_pages=_tender_detect_pages,
    row_prompt=TENDER_BIDLIST_PROMPT,
    name_key="name",
    extract_meta=_tender_extract_meta,
)


# ─── DraftRow → TenderAnchor ─────────────────────────────────────────────────

def _draft_row_to_anchor(draft_row: DraftRow, default_category: str = "阀门") -> TenderAnchor | None:
    f = draft_row.fields
    name = f.get("name") or ""
    seq = f.get("seq")
    if not name or seq in (None, ""):
        return None
    materials = f.get("materials") or {}
    materials = {
        k: str(v).strip()
        for k, v in materials.items()
        if isinstance(v, str) and v.strip()
    }
    qty = f.get("qty")
    anchor = TenderAnchor(
        seq=seq,
        name=name,
        spec=f.get("spec") or "",
        model=f.get("model") or "",
        pressure=f.get("pressure") or "",
        materials=materials,
        unit=f.get("unit") or "",
        qty=qty,
        brand=f.get("brand") or "",
        profession=f.get("profession") or "",
        remark=f.get("remark") or "",
        source_ref=draft_row.source_ref.to_dict(),
    )
    anchor.canonical = extract_valve_canonical(
        anchor.name, anchor.spec, anchor.pressure, anchor.material_text()
    )
    return anchor


# ─── 质量指标格式转换（兼容旧 quality_metrics 输出） ─────────────────────────

def _build_quality_metrics(items_out: list[dict], page_metrics: list) -> dict:
    """从 anchor items + PageMetric 列表生成兼容旧格式的 quality_metrics dict。"""
    n = len(items_out)
    if n == 0:
        return {
            "seq_missing": [], "seq_duplicate": [],
            "material_columns_filled_rate": 0.0, "brand_filled_rate": 0.0,
            "source_ref_coverage": 0.0, "qty_parse_success_rate": 0.0,
            "row_count_by_page": {}, "table_grid_pages": [], "html_fallback_pages": [],
        }

    seqs = [str(it.get("seq", "")).strip() for it in items_out if it.get("seq") is not None]
    seq_counts: dict[str, int] = {}
    for s in seqs:
        seq_counts[s] = seq_counts.get(s, 0) + 1
    seq_duplicate = sorted(s for s, c in seq_counts.items() if c > 1)
    numeric_seqs = sorted(int(s) for s in seqs if s.isdigit())
    seq_missing: list[str] = []
    if numeric_seqs:
        full_range = set(range(numeric_seqs[0], numeric_seqs[-1] + 1))
        seq_missing = sorted(str(s) for s in (full_range - set(numeric_seqs)))

    mat_filled   = sum(1 for it in items_out if any((it.get("materials") or {}).values()))
    brand_filled = sum(1 for it in items_out if str(it.get("brand") or "").strip())
    src_ref_ok   = sum(1 for it in items_out if it.get("source_ref"))
    qty_ok       = sum(1 for it in items_out if it.get("qty") is not None)

    row_count_by_page: dict[str, int] = {}
    for it in items_out:
        page = str((it.get("source_ref") or {}).get("page", "?"))
        row_count_by_page[page] = row_count_by_page.get(page, 0) + 1

    tg_pages = [m.page for m in page_metrics if m.input_mode == "table_grid"]
    fb_pages = [
        {"page": m.page, "reason": m.fallback_reason}
        for m in page_metrics
        if m.input_mode == "html_fallback"
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
    # VL-direct 优先。招标侧与报价侧共用同一套解析与结构门，差异只在列表与字段
    # （apps/api/intelligence/vl_tender.py）。legacy 保留给不具备多图调用的 provider——
    # 它同时还承担 Excel 对账（xlsx_path）与品牌页，那两项 VL 侧尚未实现。
    use_vl = hasattr(provider, "vl_extract_csv") and xlsx_path is None
    if use_vl:
        from apps.api.core.config import get_settings
        from apps.api.intelligence.vl_tender import parse_tender_document

        s = get_settings()
        parsed = parse_tender_document(
            file_path,
            vl_call=lambda imgs, prompt: provider.vl_extract_csv(
                imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL),
            orient_call=lambda parts, prompt: provider.vl_extract_csv(
                [b for _t, b in parts], prompt,
                model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
            progress_cb=progress_cb,
            target_pages=bidlist_pages or None,
        )
        draft = parsed.draft
    else:
        if not (hasattr(provider, "ocr_pages_with_roles")
                and hasattr(provider, "_llm_call_json")):
            raise RuntimeError(
                "tender_pdf.extract_bidlist 需要具备 vl_extract_csv 的 provider，"
                "或 DashScopeOCRProvider（OCR 能力）。请配置 DASHSCOPE_API_KEY。"
            )
        log.info("招标清单走 legacy：provider=%s xlsx_path=%s",
                 type(provider).__name__, bool(xlsx_path))
        draft = recognize_tables(
            file_path=file_path,
            provider=provider,
            adapter=TENDER_ADAPTER,
            progress_cb=progress_cb,
            xlsx_path=xlsx_path,
            target_pages=bidlist_pages or None,
        )

    # ── 品牌表（从 meta 取，extract_meta 已填充）──────────────────────────
    meta = draft.meta or {}
    brand_requirement = meta.get("brand_requirement") or []
    supplier_brands = meta.get("supplier_brands") or []
    material_class = meta.get("material_class") or ""
    detected_brand_page = brand_page if brand_page is not None else meta.get("brand_page")

    # ── DraftRow → TenderAnchor ───────────────────────────────────────────
    anchors: list[TenderAnchor] = []
    for row in draft.rows:
        if row.row_type != "quote_line":
            continue
        anchor = _draft_row_to_anchor(row, default_category)
        if anchor is not None:
            anchors.append(anchor)

    items_out = [anchor_to_json(a, default_category) for a in anchors]

    # ── Quality metrics（兼容旧格式）──────────────────────────────────────
    page_metrics = draft.quality.page_metrics
    quality_metrics = _build_quality_metrics(items_out, page_metrics)

    # page_diagnostics: 兼容旧格式 dict list
    page_diagnostics = [
        {
            "page": m.page,
            "input_mode": m.input_mode,
            "fallback_reason": m.fallback_reason,
            "expected_rows": m.expected_rows,
            "extracted_rows": m.extracted_rows,
            "thinking_retry": m.thinking_retry,
        }
        for m in page_metrics
    ]

    # detected_pages: bidlist 来自 draft，brand 来自 meta
    detected_bidlist = draft.target_pages

    _cat_counts = Counter(item["category"] for item in items_out if item.get("category"))
    detected_category = _cat_counts.most_common(1)[0][0] if _cat_counts else ""

    # 封面四标量。比价链路目前不消费它们，但仍然返回——供应商推荐将来会用到，
    # 且**同一份解析器对同一种文档应当产出同样的东西**，不该因为下游暂时不读就不给。
    tender_meta = (draft.meta or {}).get("tender_meta") or {}

    return {
        **{k: tender_meta.get(k, "") for k in
           ("project_name", "project_code", "tender_date", "deadline")},
        "items": items_out,
        "brand_requirement": brand_requirement,
        "supplier_brands": supplier_brands,
        "material_class": material_class,
        "detected_category": detected_category,
        "detected_pages": {"bidlist": detected_bidlist, "brand": detected_brand_page},
        "row_count": len(items_out),
        "source_type": "pdf_primary",
        "page_diagnostics": page_diagnostics,
        "quality_metrics": quality_metrics,
        "reconcile": draft.reconcile,
    }
