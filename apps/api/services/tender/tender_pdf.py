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

from apps.api.intelligence.extraction_draft import DETAIL_ROW_TYPE, DraftRow
from apps.api.services.tender.tender_list import TenderAnchor, anchor_to_json
from apps.api.services.ingestion.canonical import extract_valve_canonical

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
            "row_count_by_page": {},
            "vl_direct_pages": [], "table_grid_pages": [], "html_fallback_pages": [],
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

    # 评审 R2（第4块）：input_mode 现在生产上恒为 "vl_direct"（vl_quote.py:482），
    # table_grid/html_fallback 是已删除的 legacy 逐页链路的遗留值——tg_pages/
    # fb_pages 在当前架构下永远是空列表，前端"识别路径"那行因此永远不显示，
    # 不是 bug 触发不了，是这两个列表本身已经名不副实。补一个 vl_direct_pages，
    # 前两个保留（旧快照回放/legacy 数据仍可能带 table_grid/html_fallback）。
    vl_pages = [m.page for m in page_metrics if m.input_mode == "vl_direct"]
    tg_pages = [m.page for m in page_metrics if m.input_mode == "table_grid"]
    fb_pages = [
        {"page": m.page, "reason": m.fallback_reason}
        for m in page_metrics
        if m.input_mode == "html_fallback"
    ]
    # docs/design/25（轨A）：文字层直抽的 PageMetric.input_mode 是 "text_layer"，
    # 不匹配上面三个既有分类——不补的话这批页会在诊断里"消失"（vl/tg/fb 都不含
    # 它们），不是没触发，是分类表没跟上新来源。
    tl_pages = [m.page for m in page_metrics if m.input_mode == "text_layer"]

    return {
        "seq_missing": seq_missing,
        "seq_duplicate": seq_duplicate,
        "material_columns_filled_rate": round(mat_filled / n, 3),
        "brand_filled_rate": round(brand_filled / n, 3),
        "source_ref_coverage": round(src_ref_ok / n, 3),
        "qty_parse_success_rate": round(qty_ok / n, 3),
        "row_count_by_page": row_count_by_page,
        "vl_direct_pages": vl_pages,
        "table_grid_pages": tg_pages,
        "html_fallback_pages": fb_pages,
        "text_layer_pages": tl_pages,
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
    # 招标扫描件 VL-direct = PaddleOCR-VL（design/26 P4 补，2026-08-13）。
    # MockProvider 是命名的测试替身（`pipeline.py::extract_quote`/`extract_
    # tender` 同款分支的完整理由），继续走旧的 vision-shaped 调用契约——现有
    # 集成测试（`test_invite_integration.py` 等）依赖它的 canned 招标 CSV/meta。
    from apps.api.intelligence.providers.mock import MockProvider

    is_mock = isinstance(provider, MockProvider)
    if not is_mock and not hasattr(provider, "vl_extract_csv"):
        raise RuntimeError(
            "tender_pdf.extract_bidlist 需要具备 vl_extract_csv 的 provider。"
            "legacy OCR→HTML 识别链已于 2026-08-11 删除（最佳实践评审 F1：两个"
            "生产 provider 均实现 vl_extract_csv，该分支在生产从未可达）。"
            "请配置 DASHSCOPE_API_KEY 或使用具备 vl_extract_csv 的 provider。"
        )

    from apps.api.core.config import get_settings

    s = get_settings()

    def _vl_call(imgs, prompt):
        return provider.vl_extract_csv(imgs, prompt, model=s.DASHSCOPE_QUOTE_VL_MODEL)

    # docs/design/25（轨A）：原生 PDF（有可用文字层）走确定性文字层直抽，完全
    # 不调用视觉模型；只在 §检测 判无文字层，或抽出来的结构不可信（返回 None）
    # 时整份回落下面的 VL-direct 路径——不是"部分表格结构化、部分兜底"的混合，
    # 是文档级的二选一。手动指定过 bidlist_pages/brand_page 时不尝试这条路：
    # 用户手动修正意味着已经在用 VL 路径的页面/品牌页覆盖机制排查问题，此时
    # 悄悄换一条抽取路径会让"修正"失去对象。
    #
    # 遗留缺口（design/26 P4 补，尚未完成）：轨A命中时，采购清单走确定性文字层
    # 抽取（不碰模型），但招标要求（品牌等）仍然走 `_vl_call`（qwen 视觉调用）
    # ——`tender_text_layer.py` 还没切到 `paddle_doc_meta` 的纯文字抽取路径。
    # 这意味着 qwen 暂时还不能整体删除：轨A命中的原生 PDF 招标要求这一小段
    # 调用仍然依赖它。留作下一轮：把 `parse_tender_document_text_layer` 的
    # 要求抽取也换成喂 pdfplumber 原生文字给 `paddle_doc_meta.
    # extract_requirements_from_text`，跟本模块 VL-direct 分支这里同一个模式。
    parsed = None
    if bidlist_pages is None and brand_page is None:
        from apps.api.intelligence.tender_text_layer import (
            has_usable_text_layer, parse_tender_document_text_layer,
        )
        if has_usable_text_layer(file_path):
            try:
                parsed = parse_tender_document_text_layer(
                    file_path, vl_call=_vl_call, progress_cb=progress_cb)
            except Exception:                                    # noqa: BLE001
                log.warning("tender_text_layer 抽取异常，回落 VL-direct", exc_info=True)
                parsed = None
            if parsed is not None:
                log.info("tender_pdf.extract_bidlist: 文字层直抽命中，跳过 VL-direct")

    if parsed is None and is_mock:
        from apps.api.intelligence.vl_tender import parse_tender_document
        parsed = parse_tender_document(
            file_path,
            vl_call=_vl_call,
            orient_call=lambda parts, prompt: provider.vl_extract_csv(
                [b for _t, b in parts], prompt,
                model=s.DASHSCOPE_QUOTE_ORIENT_MODEL, labels=[t for t, _b in parts]),
            progress_cb=progress_cb,
            target_pages=bidlist_pages or None,
        )
    elif parsed is None:
        # 扫描招标件（没有可用文字层，或用户手动指定了页范围）：Paddle 是
        # 唯一路径，不再经过 `provider`/`LLMProvider`（理由同
        # `pipeline.py::extract_quote`）。手动指定的 bidlist_pages/brand_page
        # 目前对 Paddle 路径不生效——Paddle 走整份文档结构化识别，没有
        # "只送这几页"的概念；沿用整份识别结果，跟 VL-direct 手动指定页范围
        # 时"仍然整份渲染、只是清单只认这几页"的既有行为不冲突。
        from apps.api.intelligence.document_loader import DocumentLoader
        from apps.api.intelligence.paddle_doc_meta import get_text_client_call
        from apps.api.intelligence.paddle_tender import parse_tender_document_paddle
        from apps.api.intelligence.providers import paddle_ocr

        page_count = DocumentLoader.get_page_count(file_path)
        parsed = parse_tender_document_paddle(
            file_path, submit_and_parse=paddle_ocr.submit_and_parse,
            text_call=get_text_client_call(), page_count=page_count,
            progress_cb=progress_cb,
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
    # DETAIL_ROW_TYPE（评审 N2）：字面值历史上叫 "quote_line"，招标清单里没有
    # 一行是报价，用具名常量而不是字符串字面量，标明这里判的是"是不是小计/
    # 合计行"，与 quote 语义无关。
    anchors: list[TenderAnchor] = []
    for row in draft.rows:
        if row.row_type != DETAIL_ROW_TYPE:
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

    # 封面标量。比价链路目前不消费它们，但仍然返回——供应商推荐将来会用到，
    # 且**同一份解析器对同一种文档应当产出同样的东西**，不该因为下游暂时不读就不给。
    # （design/29 §10 起 tenderer/招标单位有了真实消费方：工作台卡片的单位名称。）
    from apps.api.intelligence.vl_tender import _META_KEYS

    tender_meta = (draft.meta or {}).get("tender_meta") or {}

    return {
        # 键集合以 vl_tender._META_KEYS 为准，不在这里再抄一份字面量——
        # 招标单位（tenderer）加进去时这里漏改就是一次静默丢字段。
        **{k: tender_meta.get(k, "") for k in _META_KEYS},
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
