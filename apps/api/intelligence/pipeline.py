"""ExtractionPipeline — orchestrates loader → provider → post-processing.

Two public methods:
- extract_tender(file_path) → dict matching TENDER_SCHEMA
- extract_quote(file_path, context) → dict matching QUOTE_SCHEMA

Post-processing:
- coerces numeric fields (qty / unit_price / total_price)
- strips whitespace on strings
- best-effort category inference for tender items
"""

from __future__ import annotations

import logging
import re
from typing import Any

from apps.api.intelligence.base import LLMProvider, ExtractionResponse
from apps.api.intelligence.document_loader import DocumentLoader
from apps.api.intelligence.prompts import TENDER_PROMPT, QUOTE_PROMPT
from apps.api.intelligence.schemas import TENDER_SCHEMA, QUOTE_SCHEMA

log = logging.getLogger(__name__)

# Used by category inference; matches apps/api/core/config.py ALL_CATEGORIES
KNOWN_CATEGORIES = [
    "桥架", "母线槽", "配电箱", "阀门", "不锈钢管",
    "水箱", "潜水泵", "风口风阀", "风机盘管", "空调泵",
]


class ExtractionPipeline:
    """Coordinates document loading, LLM call, and structured post-processing."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    # ─── public API ───────────────────────────────────────────────────────
    def extract_tender(self, file_path: str) -> ExtractionResponse:
        images = DocumentLoader.to_images(file_path)
        resp = self.provider.extract(images, TENDER_SCHEMA, TENDER_PROMPT)
        resp.data = self._postprocess_tender(resp.data)
        return resp

    def extract_quote(
        self, file_path: str, context: dict[str, Any] | None = None
    ) -> ExtractionResponse:
        images = DocumentLoader.to_images(file_path)
        resp = self.provider.extract(images, QUOTE_SCHEMA, QUOTE_PROMPT)
        resp.data = self._postprocess_quote(resp.data, context or {})
        return resp

    # ─── post-processing ──────────────────────────────────────────────────
    @staticmethod
    def _postprocess_tender(data: dict) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = (it.get("name") or "").strip()
            if not name:
                continue
            category = (it.get("category") or "").strip()
            if not category:
                category = _infer_category(name)
            cleaned.append({
                "name": name,
                "category": category,
                "spec": (it.get("spec") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "quantity": _coerce_num(it.get("quantity")),
                "remark": (it.get("remark") or "").strip(),
            })
        return {
            "project_name": (data.get("project_name") or "").strip(),
            "project_code": (data.get("project_code") or "").strip(),
            "tender_date": (data.get("tender_date") or "").strip(),
            "deadline": (data.get("deadline") or "").strip(),
            "items": cleaned,
        }

    @staticmethod
    def _postprocess_quote(data: dict, ctx: dict[str, Any]) -> dict:
        items = data.get("items") or []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            material = (it.get("material") or "").strip()
            if not material:
                continue
            price = _coerce_num(it.get("unit_price"))
            qty = _coerce_num(it.get("qty"))
            total = _coerce_num(it.get("total_price"))
            if total is None and price is not None and qty is not None:
                total = round(price * qty, 4)
            cleaned.append({
                "material": material,
                "spec": (it.get("spec") or "").strip(),
                "brand": (it.get("brand") or "").strip(),
                "unit": (it.get("unit") or "").strip(),
                "qty": qty,
                "unit_price": price,
                "unit_price_excl_tax": _coerce_num(it.get("unit_price_excl_tax")),
                "total_price": total,
                "tax_rate": _coerce_num(it.get("tax_rate")),
                "remark": (it.get("remark") or "").strip(),
            })
        return {
            "supplier_name": (data.get("supplier_name") or "").strip(),
            "quote_date": (data.get("quote_date") or "").strip(),
            "items": cleaned,
            "context": ctx,
        }


# ─── helpers ──────────────────────────────────────────────────────────────
def _coerce_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in {".", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _infer_category(name: str) -> str:
    """Heuristic: scan material name for a known category keyword."""
    for cat in KNOWN_CATEGORIES:
        if cat in name:
            return cat
    return ""
