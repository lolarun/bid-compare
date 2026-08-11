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

from apps.api.core.domain_config import MATCH_PRICE_ARITHMETIC_TOLERANCE as _PRICE_TOL
from dataclasses import dataclass, field
from typing import Any


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

    # ── 合价来源三态（doc/19 §L2）──────────────────────────────────────────
    # ocr      原文读到
    # manual   原文没有、用户明确补写
    # missing  原文没有、也没人工补写 → 权威值保持 None，只留候选
    total_source: str = ""                  # 由 __post_init__ 判定，不由构造方传入
    derived_total_candidate: float | None = None   # 数量×单价的候选值，**不是权威金额**

    # ── post-init: 判定合价来源，不写权威值 ─────────────────────────────────
    def __post_init__(self) -> None:
        """原文没有合价时**只留候选，不写权威值**。

        2026-08-09 修正：这里原本直接 `total_price = unit_price * qty`。派生发生在
        构造函数里，任何创建 QuoteFact 的路径都会中招，而且写进去之后**事后无法分辨
        这个数是读来的还是算的**——下游的算术校验 |qty×price − total| 因此恒为 0，
        把列错位、漏读单元格这类真实缺陷全部洗白。

        现在权威 `total_price` 保持 None，派生值只进 `derived_total_candidate`，
        `total_source` 标 `missing`。入库门据此阻断并要求人工补写（doc/19 §L2）。
        与 pipeline.py 的派生入口保持同一口径（rebuild_submission_lines.py 已删除，
        见最佳实践评审 D1 —— 它是确认写入路径的克隆且漏了 price_basis 桥接，
        唯一调用方是已下线的一次性修复脚本 repair_project63.py）。
        """
        if self.total_price is not None:
            self.total_source = "ocr"
            return
        self.total_source = "missing"
        if self.unit_price is not None and self.qty is not None:
            self.derived_total_candidate = round(self.unit_price * self.qty, 4)

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
            # 来源标记必须随行走。少了它，下游拿到 total_price=None 只知道"没有"，
            # 不知道"原文就没有"还是"读丢了"，也拿不到候选值给人工参考。
            "total_source": self.total_source,
            "derived_total_candidate": self.derived_total_candidate,
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
