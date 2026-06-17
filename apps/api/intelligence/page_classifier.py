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

from dataclasses import dataclass
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
