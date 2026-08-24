"""scanned_pdf_classify.py — Tier 1.5：扫描件招标/投标粗判定。design/29 §3。

design/28 的 Tier 0（`document_classify.py`）对 PDF 只能判"原生文字层 vs
扫描件"，判不出招标/投标——那需要看封面内容。原生 PDF 有文字层，可以零模型
调用地读前两页纯文字（跟 `tender_text_layer.has_usable_text_layer` 同一套
pypdfium2 读法，本模块自己实现，那个模块本身只负责表格结构抽取）+ 关键词
判据（跟 Tier 0 对 Excel 用的思路一样，见 `classify_native_pdf`）；扫描件
没有文字层，唯一办法是花一次不大的视觉调用——送前几页原生分辨率图（不是
整份文档识别，那是识别管线本身要做的事，这里只是"先看一眼封面+投标函，
决定该走哪条识别管线"）。

**2026-08-21 实测修正**：最初版本只送第一页缩略图，7 份真实投标扫描 PDF
实测 **0/7**——不是模型读不懂，是给的信息量不够（详见
`DashScopeOCRProvider.classify_document_kind` 的方法文档）。改成送前
`SCANNED_CLASSIFY_PAGES` 张原生分辨率图 + 点破常见易错点的提示词，同一批
语料复测 **8/8**（7 份投标 + 1 份招标全对）。

这一级判定发生在**识别之前**，用于决定该走 `paddle_tender.py` 还是
`paddle_vl.py`（招标/报价两条管线的提取 schema 不同，不能识别完再选）——
跟 design/28 §3 定义的 Tier 1（`tier1_signals.py`，识别**之后**看产物形状）
是两个不同阶段，故意叫 1.5 而不是塞进 Tier 1，避免混淆"之前"还是"之后"。

"不确定"是合法答案，不是失败——判不出来就诚实弹出人工二选一，不强行分派
到某条管线（design/28 §1 红线 3 同一条原则："低置信度必须标注，不能猜"）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Literal

from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.tender_text_layer import has_usable_text_layer

log = logging.getLogger(__name__)

Tier15Verdict = Literal["tender", "bid", "uncertain"]

# 招标/投标封面关键词——跟 Tier 0 对 Excel 表头做槽位匹配同一个"确定性关键
# 词判据"思路，用在原生 PDF 已抽出的文字上，零模型调用。任一侧命中且另一侧
# 未命中才判定；两侧都命中或都未命中一律 uncertain，不猜哪个更像。
_TENDER_KEYWORDS = ("招标文件", "招标编号", "招标人", "采购清单", "投标须知", "招标公告")
_BID_KEYWORDS = ("投标文件", "投标函", "投标单位", "投标人", "法定代表人授权", "报价函")


@dataclass
class Tier15Result:
    verdict: Tier15Verdict
    method: Literal["native_text_keyword", "scanned_vl"]  # 走的哪条判据路径，供审计/measure脚本统计准确率分路径
    project_name_hint: str = ""
    supplier_name_hint: str = ""
    evidence: list[str] = field(default_factory=list)
    reason: str = ""


# 扫描件路径的注入点——(多页图片字节列表) -> 原始解析结果 dict。跟项目里
# TextCall/VLCall 同一个"可注入、不内嵌网络调用"约定，方便单测不用真的发请求。
ScannedClassifyCall = Callable[[list[bytes]], dict]

# 送几页给视觉分类——2026-08-21 实测：1 页（缩略图）0/7，3 页（原生分辨率）
# 8/8。招标封面被投标方原样重印是最初失败的根因（见模块 docstring），投标函/
# 授权书这类只会出现在投标文件里的页面通常在第 2-3 页，多送这几页能让模型
# 看到不会跟招标模板混淆的内容。
SCANNED_CLASSIFY_PAGES = 3


def get_scanned_classify_call() -> ScannedClassifyCall | None:
    """生产客户端：`DashScopeOCRProvider.classify_document_kind`。没配置
    DASHSCOPE_API_KEY 时返回 None，调用方按"判不出来"处理，不抛异常
    （跟 `paddle_doc_meta.get_text_client_call` 同一个失败不拖垮主线约定）。"""
    from apps.api.core.config import get_settings
    from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider, ProviderError

    # design/41：视觉供应商由 `VISION_CLIENT_VENDOR` 决定，默认 dashscope。
    # 配 mimo 但没 key 时**明确回落并记日志**，不静默降级。
    from apps.api.core.domain_config import VISION_CLIENT_VENDOR

    if VISION_CLIENT_VENDOR == "mimo":
        from apps.api.intelligence.providers.mimo_vision import get_mimo_vision_provider

        mimo = get_mimo_vision_provider()
        if mimo is not None:
            return mimo.classify_document_kind
        log.warning("VISION_CLIENT_VENDOR=mimo 但没有 MIMO_API_KEY，回落 dashscope")

    settings = get_settings()
    if not settings.DASHSCOPE_API_KEY:
        return None
    try:
        provider = DashScopeOCRProvider(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL,
            ocr_model=settings.DASHSCOPE_OCR_MODEL,
            llm_model=settings.DASHSCOPE_LLM_MODEL,
        )
    except ProviderError:
        return None
    return provider.classify_document_kind


def _read_first_pages_text(file_path: str, max_pages: int = 2) -> str:
    """前几页纯文字层读取——跟 `has_usable_text_layer` 同一套 pypdfium2 读法
    （零渲染、零模型调用），本模块自用，不是 `tender_text_layer.py` 的一部分
    （那个模块只负责表格结构抽取，没有裸文字导出接口）。"""
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(file_path)
        try:
            n = min(len(doc), max_pages)
            return "\n".join(doc[i].get_textpage().get_text_range() for i in range(n))
        finally:
            doc.close()
    except Exception:                                              # noqa: BLE001
        log.warning("_read_first_pages_text 读取失败", exc_info=True)
        return ""


# 封面/标题区域跟目录之间的实测边界（金桥/prj2 两份真实招标 PDF 都在
# 211-221 字符处出现"目录"）——关键词判据必须限定在目录**之前**，招标
# 文件的目录本身会列"第X章 投标须知""投标资格预审文件"这类章节标题，
# 章节名里天然带"投标"字样，不代表文档本身是投标文件；若不限定范围，
# 两侧关键词几乎总是同时命中，判定退化成永远 uncertain（这是实测复现出
# 来的真 bug，不是假设）。
_COVER_REGION_MAX_CHARS = 250


def classify_native_pdf(file_path: str) -> Tier15Result:
    """原生文字层 PDF：零模型调用，关键词判据只看封面区域（目录之前），
    不看整份前两页——原因见 `_COVER_REGION_MAX_CHARS` 注释。"""
    full_text = _read_first_pages_text(file_path)
    toc_idx = full_text.find("目录")
    cover_end = toc_idx if toc_idx > 0 else _COVER_REGION_MAX_CHARS
    text = full_text[:min(cover_end, _COVER_REGION_MAX_CHARS)]
    tender_hit = any(kw in text for kw in _TENDER_KEYWORDS)
    bid_hit = any(kw in text for kw in _BID_KEYWORDS)
    if tender_hit and not bid_hit:
        return Tier15Result("tender", "native_text_keyword",
                            reason="封面区域（目录之前）命中招标关键词、未命中投标关键词。")
    if bid_hit and not tender_hit:
        return Tier15Result("bid", "native_text_keyword",
                            reason="封面区域（目录之前）命中投标关键词、未命中招标关键词。")
    if tender_hit and bid_hit:
        return Tier15Result("uncertain", "native_text_keyword",
                            reason="封面区域招标/投标关键词都命中，不能确定哪个是文档本身的类型。")
    return Tier15Result("uncertain", "native_text_keyword",
                        reason="封面区域未命中任何招标/投标关键词。")


def classify_scanned_pdf(file_path: str, call: ScannedClassifyCall | None) -> Tier15Result:
    """扫描件 PDF：送前 `SCANNED_CLASSIFY_PAGES` 页原生分辨率图给 `call`
    （不是缩略图——2026-08-21 实测缩略图+单页是 0/7 的根因之一）。
    `call=None`（未配置视觉客户端）时直接判 uncertain，不假装判断过。"""
    if call is None:
        return Tier15Result("uncertain", "scanned_vl", reason="未配置视觉分类客户端。")
    pages = DocumentLoader.to_images(file_path, max_pages=SCANNED_CLASSIFY_PAGES)
    if not pages:
        return Tier15Result("uncertain", "scanned_vl", reason="无法渲染页面。")
    data = call(pages)
    n = len(pages)
    return Tier15Result(
        verdict=data.get("doc_type", "uncertain"),
        method="scanned_vl",
        project_name_hint=data.get("project_name_hint", ""),
        supplier_name_hint=data.get("supplier_name_hint", ""),
        evidence=data.get("evidence", []),
        reason=(f"视觉分类前{n}页：" + "；".join(data.get("evidence", []))
                if data.get("evidence") else f"视觉分类前{n}页。"),
    )


def classify_pdf_for_dispatch(file_path: str, call: ScannedClassifyCall | None) -> Tier15Result:
    """统一入口——按有没有可用文字层分流到零模型调用或视觉调用两条路径。
    调用方（工作台自动分类拖拽区）不需要关心走的哪条，只看返回的 verdict。"""
    if has_usable_text_layer(file_path):
        return classify_native_pdf(file_path)
    return classify_scanned_pdf(file_path, call)
