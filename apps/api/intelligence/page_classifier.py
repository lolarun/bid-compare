"""Page classifier — classify OCR HTML pages by role.

Lightweight code-only classifier (no LLM call). Called after Stage 1 OCR
to decide which pages skip Stage 2 extraction.

Design principle: QUOTE_TABLE is the DEFAULT for anything that might contain
quote items. Only skip pages we are CONFIDENT are non-quote (covers, summaries,
certificates, bid letters).

Qwen-VL table_parsing returns:
  - HTML with <tr> tags: for pages that have formatted tables
  - Plain text / markdown: for pages that have mostly prose

We must handle BOTH formats. A page with price keywords in plain text is still
a quote page worth sending to Stage 2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PageRole(str, Enum):
    QUOTE_TABLE = "quote_table"
    COVER = "cover"
    SUMMARY = "summary"
    BID_LETTER = "bid_letter"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass
class PageClassification:
    primary_role: PageRole
    has_doc_total: bool = False    # page contains a document-level total price
    has_supplier_name: bool = False
    confidence: float = 1.0


# ── Visual page classification (qwen3-vl-flash/plus) ────────────────────────
# 投产页面分类的角色枚举。与旧 PageRole（纯规则、粗粒度）并存：旧的保留为硬规则
# 兜底；新的由视觉模型产生，是 recognize_tables 的路由依据。
class VisualPageRole(str, Enum):
    COVER = "cover"
    BID_LETTER = "bid_letter"
    TENDER_TABLE_HEADER = "tender_table_header"
    TENDER_TABLE_CONTINUATION = "tender_table_continuation"
    QUOTE_TABLE_HEADER = "quote_table_header"
    QUOTE_TABLE_CONTINUATION = "quote_table_continuation"
    SUBTOTAL_OR_SUMMARY = "subtotal_or_summary"
    BRAND_REQUIREMENT = "brand_requirement"
    TECHNICAL_SPEC = "technical_spec"
    COMPONENT_PARAMETER_TABLE = "component_parameter_table"
    CERTIFICATE = "certificate"
    OTHER = "other"
    UNKNOWN = "unknown"            # 解析失败/未决（不得直接当目标页）


# 路由集合（§五）
QUOTE_TARGET_ROLES = {
    VisualPageRole.QUOTE_TABLE_HEADER, VisualPageRole.QUOTE_TABLE_CONTINUATION,
}
TENDER_TARGET_ROLES = {
    VisualPageRole.TENDER_TABLE_HEADER, VisualPageRole.TENDER_TABLE_CONTINUATION,
}
# 需 OCR 取文本作元信息（声明总价/品牌），但不产商品行
META_ROLES = {
    VisualPageRole.SUBTOTAL_OR_SUMMARY, VisualPageRole.BRAND_REQUIREMENT,
}
# 禁止进入 OCR 抽取
OCR_SKIP_ROLES = {
    VisualPageRole.TECHNICAL_SPEC, VisualPageRole.COMPONENT_PARAMETER_TABLE,
    VisualPageRole.CERTIFICATE, VisualPageRole.COVER,
    VisualPageRole.BID_LETTER, VisualPageRole.OTHER,
}


@dataclass
class VisualPageClassification:
    page: int                              # 1-based
    role: VisualPageRole
    confidence: float = 0.0
    contains_table: bool = False
    orientation: int = 0                   # 0 / 90 / 180 / 270
    continues_from_page: int | None = None
    mixed_content: bool = False
    evidence: list[str] = field(default_factory=list)
    source: str = "flash"                  # flash | plus | rule
    # v3 语义结构字段 — 用于代码级替代链长度启发式
    has_line_items: bool | None = None         # 有逐条明细行（品名/数量/单价多列）
    estimated_line_item_count: int | None = None  # 估计明细行数
    has_column_header: bool | None = None     # 有列名行
    has_total_row: bool | None = None         # 有合计/小计/总价行
    table_structure_continues: bool | None = None  # 列结构与前页一致

    def to_dict(self) -> dict:
        return {
            "page": self.page, "role": self.role.value, "confidence": self.confidence,
            "contains_table": self.contains_table, "orientation": self.orientation,
            "continues_from_page": self.continues_from_page,
            "mixed_content": self.mixed_content, "evidence": self.evidence,
            "source": self.source,
            "has_line_items": self.has_line_items,
            "estimated_line_item_count": self.estimated_line_item_count,
            "has_column_header": self.has_column_header,
            "has_total_row": self.has_total_row,
            "table_structure_continues": self.table_structure_continues,
        }

    @staticmethod
    def from_dict(d: dict) -> "VisualPageClassification":
        try:
            role = VisualPageRole(d.get("role", "unknown"))
        except ValueError:
            role = VisualPageRole.UNKNOWN
        ori = d.get("orientation", 0)
        if ori not in (0, 90, 180, 270):
            ori = 0
        return VisualPageClassification(
            page=int(d.get("page", 0)), role=role,
            confidence=float(d.get("confidence", 0.0)),
            contains_table=bool(d.get("contains_table", False)),
            orientation=ori,
            continues_from_page=d.get("continues_from_page"),
            mixed_content=bool(d.get("mixed_content", False)),
            evidence=list(d.get("evidence", []) or []),
            source=d.get("source", "flash"),
            has_line_items=d.get("has_line_items"),
            estimated_line_item_count=d.get("estimated_line_item_count"),
            has_column_header=d.get("has_column_header"),
            has_total_row=d.get("has_total_row"),
            table_structure_continues=d.get("table_structure_continues"),
        )


_QUOTE_PRICE_SIGNALS = ["单价", "合价", "综合单价"]
_QUOTE_CONTENT_SIGNALS = [
    "规格", "数量", "单位", "材料", "名称", "品名", "型号",
    "截止阀", "闸阀", "球阀", "蝶阀", "止回阀", "减压阀", "安全阀",
    "过滤器", "阀门", "管件", "DN",
]
_BID_LETTER_SIGNALS = ["投标函", "法定代表人授权", "授权委托书"]
_SUMMARY_SIGNALS = ["汇总表", "报价汇总", "费用汇总"]
_CERT_SIGNALS = ["营业执照", "资质证书", "安全生产许可", "统一社会信用代码", "社会统一信用代码", "注册资本", "证照编号"]
_DOC_TOTAL_SIGNALS = ["投标总价", "报价总额", "总报价"]
_SUPPLIER_NAME_SIGNALS = ["投标单位", "报价单位", "投标人"]


def classify_page(html: str) -> PageClassification:
    """Classify a page's OCR output (HTML or plain text) into a PageRole.

    Strategy: first identify pages we are CONFIDENT are NOT quote tables
    (covers, cert pages, bid letters, summaries). Everything else goes to
    Stage 2 as a potential quote table.

    This prevents false exclusions — it's much safer to over-include pages
    in Stage 2 (the LLM handles garbage gracefully) than to miss quote pages.
    """
    # ── Structural signals ────────────────────────────────────────────────
    tr_count = html.count("<tr")
    has_price = any(s in html for s in _QUOTE_PRICE_SIGNALS)
    has_content = sum(1 for s in _QUOTE_CONTENT_SIGNALS if s in html) >= 1

    # ── 1. Summary: check early to avoid misclassifying as quote ─────────
    #     "汇总表" is a category-level total, not line-item detail.
    if any(s in html for s in _SUMMARY_SIGNALS) and not has_price:
        primary = PageRole.SUMMARY
        confidence = 0.90

    # ── 2. Certificate / qualification docs — clear non-quote pages ──────
    elif any(s in html for s in _CERT_SIGNALS) and not has_price:
        primary = PageRole.OTHER
        confidence = 0.90

    # ── 3. Bid letter ────────────────────────────────────────────────────
    elif any(s in html for s in _BID_LETTER_SIGNALS) and not has_price:
        primary = PageRole.BID_LETTER
        confidence = 0.90

    # ── 4. Cover: BOTH signals required; must NOT have price table ────────
    elif ("投标总价" in html and any(s in html for s in _SUPPLIER_NAME_SIGNALS)
          and tr_count < 4 and not (has_price and has_content)):
        primary = PageRole.COVER
        confidence = 0.90

    # ── 5. Hard-positive: strong HTML table structure + price signals ─────
    elif tr_count >= 4 and has_price:
        primary = PageRole.QUOTE_TABLE
        confidence = 0.97

    # ── 6. Price signals (with or without HTML table structure) ──────────
    #     A page mentioning "单价/合价" is almost certainly a quote page.
    elif has_price and has_content:
        primary = PageRole.QUOTE_TABLE
        confidence = 0.88

    elif has_price and tr_count >= 2:
        primary = PageRole.QUOTE_TABLE
        confidence = 0.80

    elif has_price:
        # Price signals alone (no table/content) → still likely quote
        primary = PageRole.QUOTE_TABLE
        confidence = 0.72

    # ── 7. Has content signals (materials, specs, DN) → likely quote ────
    elif has_content:
        primary = PageRole.QUOTE_TABLE
        confidence = 0.68

    # ── 8. Default: send to Stage 2 rather than risk losing items ────────
    #     Any page we're unsure about gets processed; LLM returns empty
    #     items for non-quote content anyway.
    else:
        primary = PageRole.UNKNOWN
        confidence = 0.50

    # ── Flags (independent of primary role) ──────────────────────────────
    has_doc_total = any(s in html for s in _DOC_TOTAL_SIGNALS) or (
        "合计" in html and tr_count >= 2 and has_price
    )
    has_supplier_name = any(s in html for s in _SUPPLIER_NAME_SIGNALS)

    return PageClassification(
        primary_role=primary,
        has_doc_total=has_doc_total,
        has_supplier_name=has_supplier_name,
        confidence=confidence,
    )
