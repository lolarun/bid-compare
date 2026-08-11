"""招标文件 PDF → 投标清单锚点 + 品牌映射抽取。

公共入口 extract_bidlist 签名不变；内部为 VL-direct（apps/api/intelligence/vl_tender.py）。

**legacy 分支（OCR→HTML→RecognizeAdapter→recognize_tables）已删除**（2026-08-11，
最佳实践评审 F1）：两个 shipped provider（DashScopeOCRProvider、MockProvider）都
实现 vl_extract_csv，legacy 分支在生产从未可达；design/21 判定"不可达"不等于
"可交叉校验保留"，予以物理删除而非继续携带。provider 不具备 vl_extract_csv 时
直接报错，不再静默降级到一条已证明拿不到调用的路径。

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
from collections import Counter

from apps.api.intelligence.extraction_draft import DraftRow
from apps.api.services.tender_list import TenderAnchor, anchor_to_json
from apps.api.services.canonical import extract_valve_canonical

log = logging.getLogger(__name__)


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
        provider: 需具备 vl_extract_csv（DashScopeOCRProvider / MockProvider）。
        bidlist_pages / brand_page: 1-based 手动指定页（用户修正用）；None 则自动定位。
        default_category: 强制品类（本类招标文件单品类，默认阀门）。
        xlsx_path: 可选 Excel 清单路径；传入时自动运行 source reconciliation。
    """
    # VL-direct 是唯一路径。招标侧与报价侧共用同一套解析与结构门，差异只在列表与
    # 字段（apps/api/intelligence/vl_tender.py）。
    #
    # **Excel 不是降级理由。** 先前把 `xlsx_path` 作为落回 legacy 的条件是错的：
    # excel_reconcile 只吃 DraftRow，与识别器无关，VL 的行同样能对账。而且
    # 有些招标文件的 PDF 里**根本没有采购清单**，清单以 Excel 附件形式给出——
    # 那时 Excel 是唯一来源而非交叉校验，更不该因为它的存在而降级 PDF 识别。
    if not hasattr(provider, "vl_extract_csv"):
        raise RuntimeError(
            "tender_pdf.extract_bidlist 需要具备 vl_extract_csv 的 provider。"
            "legacy OCR→HTML 识别链已于 2026-08-11 删除（最佳实践评审 F1：两个"
            "生产 provider 均实现 vl_extract_csv，该分支在生产从未可达）。"
            "请配置 DASHSCOPE_API_KEY 或使用具备 vl_extract_csv 的 provider。"
        )

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
    # Excel 对账：与识别器无关（只吃 DraftRow）。失败只记录不抛——对账是校验，
    # 不是识别本身。
    if xlsx_path:
        from apps.api.intelligence.excel_reconcile import reconcile_vs_excel
        try:
            draft.reconcile = reconcile_vs_excel(
                "tender", draft.rows, xlsx_path, "name")
        except Exception as exc:                             # noqa: BLE001
            log.error("VL 路径 Excel 对账失败: %s", exc)
            draft.reconcile = {"error": str(exc)}

    # ── 招标要求（品牌等）────────────────────────────────────────────────
    # parse_tender_document 填进 draft.meta["tender_requirements"]。
    meta = draft.meta or {}
    reqs = meta.get("tender_requirements") or {}
    brand_requirement = reqs.get("brand_requirement") or meta.get("brand_requirement") or []
    supplier_brands = reqs.get("supplier_brands") or meta.get("supplier_brands") or []
    material_class = reqs.get("material_class") or meta.get("material_class") or ""
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
