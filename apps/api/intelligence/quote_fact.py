"""quote_fact.py — Unified intermediate layer for all quote ingestion formats.

Both PDF OCR (pipeline.py) and tabular (tabular_ingestion.py) converge to
QuoteFact before reaching batch-confirm / anchor-match downstream.

Contract: QuoteFact.to_item_dict() must output exactly the same keys as
pipeline._postprocess_quote produces, so that batch-confirm (quotes.py:299-608)
requires zero changes.

Required keys (must stay in sync):
    material, spec, brand, unit, qty, unit_price, unit_price_excl_tax,
    total_price, tax_rate, material_type, remark, canonical, validation_warning,
    normalized_material, ocr_correction_reason

Optional extra key (ignored by batch-confirm, used by Tier3 LLM review later):
    source_ref  — {"page": int, "table": int, "row": int} for PDF TableGrid; omitted for tabular
"""
from __future__ import annotations

import re

from apps.api.core.domain_config import MATCH_PRICE_ARITHMETIC_TOLERANCE as _PRICE_TOL
from dataclasses import dataclass, field
from typing import Any


# ─── helpers (mirror pipeline._coerce_num, kept local to avoid circular import) ──

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


# ─── shared logic (called from both pipeline.py and tabular_ingestion.py) ────

def build_canonical(
    material: str,
    spec: str,
    material_type: str = "",
    llm_canonical: dict | None = None,
    normalized_material: str = "",
) -> dict:
    """Merge regex-based code extraction with optional LLM result.

    Mirrors pipeline._postprocess_quote lines 477-484.
    LLM values override code-extracted values (LLM can correct OCR artefacts).
    For tabular ingestion pass llm_canonical=None.

    material_type (材质: 不锈钢/球墨铸铁/碳钢…) feeds the canonical `material`
    dimension so "same DN/PN, different material" can be distinguished downstream.

    normalized_material (Layer 1 OCR correction): when the LLM detected a 形近字
    OCR error and provided a corrected name, use it for code-based extraction so
    the canonical score is not killed by the corrupted raw text.
    """
    from apps.api.services.canonical import extract_valve_canonical

    effective_material = normalized_material or material
    code_canon: dict = extract_valve_canonical(effective_material, spec, material=material_type)
    canonical: dict = {k: v for k, v in code_canon.items() if v}
    if isinstance(llm_canonical, dict):
        for k, v in llm_canonical.items():
            if v:
                canonical[k] = v
    return canonical


def apply_arithmetic_validation(items: list[dict]) -> list[dict]:
    """Row-level arithmetic gate: flag rows where qty × unit_price ≠ total_price (>5%).

    Mirrors pipeline.ExtractionPipeline._validate_items (lines 441-458).
    Mutates items in-place and returns them for convenience.
    """
    for item in items:
        qty = item.get("qty")
        price = item.get("unit_price")
        total = item.get("total_price")
        if (
            qty is not None
            and price is not None
            and total is not None
            and total > 0
        ):
            computed = qty * price
            diff_ratio = abs(computed - total) / total
            if diff_ratio > _PRICE_TOL:
                item["validation_warning"] = (
                    f"金额不符: {qty}×{price:.2f}={computed:.2f}≠{total:.2f}"
                    f" (diff {diff_ratio:.1%})"
                )
    return items


# ─── QuoteFact dataclass ──────────────────────────────────────────────────────

@dataclass
class QuoteFact:
    """Unified intermediate representation of one quote line item.

    Both PDF OCR and tabular parsers must produce QuoteFact instances and call
    to_item_dict() before returning items to the shared downstream pipeline.

    Fields mirror the output contract of pipeline._postprocess_quote exactly.
    source_ref is an extension field (batch-confirm ignores it; reserved for
    future Tier3 LLM evidence tracing).
    """
    # ── required ──────────────────────────────────────────────────────────────
    material: str                           # 材料名称
    spec: str = ""                          # 规格型号
    brand: str = ""                         # 品牌
    unit: str = ""                          # 单位
    qty: float | None = None                # 数量
    unit_price: float | None = None         # 含税单价
    unit_price_excl_tax: float | None = None  # 不含税单价
    total_price: float | None = None        # 合价
    tax_rate: float | None = None           # 税率
    material_type: str = ""                 # 材质 (不锈钢/球墨铸铁/碳钢…)
    remark: str = ""                        # 备注
    canonical: dict = field(default_factory=dict)          # 结构化属性 (dn/valve_type/pn…)
    validation_warning: str = ""            # 算术校验警告 (由 apply_arithmetic_validation 写入)
    normalized_material: str = ""           # Layer 1 OCR纠错后名称 (原文保留在 material)
    ocr_correction_reason: str = ""         # 纠错依据 (词表+DN连续性)

    # ── extension (optional, batch-confirm ignores) ────────────────────────
    source_ref: dict | None = None          # {"sheet": "Sheet1", "row": 3}

    # ── post-init: derive total_price if missing ──────────────────────────────
    def __post_init__(self) -> None:
        if (
            self.total_price is None
            and self.unit_price is not None
            and self.qty is not None
        ):
            self.total_price = round(self.unit_price * self.qty, 4)

    def to_item_dict(self) -> dict:
        """Return a dict compatible with batch-confirm's expected item shape.

        Keys must stay in sync with pipeline._postprocess_quote output and with
        quotes.py:batch-confirm consumer.  source_ref is appended when present;
        batch-confirm silently ignores unknown keys (extra="ignore" on schemas).
        """
        d: dict = {
            "material": self.material,
            "spec": self.spec,
            "brand": self.brand,
            "unit": self.unit,
            "qty": self.qty,
            "unit_price": self.unit_price,
            "unit_price_excl_tax": self.unit_price_excl_tax,
            "total_price": self.total_price,
            "tax_rate": self.tax_rate,
            "material_type": self.material_type,
            "remark": self.remark,
            "canonical": self.canonical,
            "validation_warning": self.validation_warning,
            "normalized_material": self.normalized_material,
            "ocr_correction_reason": self.ocr_correction_reason,
        }
        if self.source_ref is not None:
            d["source_ref"] = self.source_ref
        return d


# ─── convenience factory ──────────────────────────────────────────────────────

def quote_fact_from_row(
    *,
    material: str,
    spec: str = "",
    brand: str = "",
    unit: str = "",
    qty: float | None = None,
    unit_price: float | None = None,
    unit_price_excl_tax: float | None = None,
    total_price: float | None = None,
    tax_rate: float | None = None,
    material_type: str = "",
    remark: str = "",
    llm_canonical: dict | None = None,
    normalized_material: str = "",
    ocr_correction_reason: str = "",
    source_ref: dict | None = None,
) -> QuoteFact:
    """Construct a QuoteFact with canonical resolution in one call.

    Intended for tabular_ingestion where all values come from cell reads.
    """
    canonical = build_canonical(
        material, spec, material_type=material_type, llm_canonical=llm_canonical,
        normalized_material=normalized_material,
    )
    return QuoteFact(
        material=material,
        spec=spec,
        brand=brand,
        unit=unit,
        qty=qty,
        unit_price=unit_price,
        unit_price_excl_tax=unit_price_excl_tax,
        total_price=total_price,
        tax_rate=tax_rate,
        material_type=material_type,
        remark=remark,
        canonical=canonical,
        normalized_material=normalized_material,
        ocr_correction_reason=ocr_correction_reason,
        source_ref=source_ref,
    )
