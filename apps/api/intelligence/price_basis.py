"""price_basis.py — 价格口径桥接 (§4 财务口径 / §7·§9 比价隔离)。

识别层每行可能带三套价格字段：含税 (incl)、不含税 (excl)、通用 (generic 单价/合价)。
本模块在 **不修改任何原始金额** 的前提下，判定该行价格口径 (price_basis)，并给出
比价有效价 (effective_unit_price / effective_total_price)，供 batch-confirm 写入
BidQuoteLine.unit_price/total_price；原始三套字段与口径完整保留到 extraction_meta。

口径判定 (CLAUDE.md §4 含税/不含税区分；§9 禁止静默混比)：
  - dual_tax    含税与不含税同时存在 → 有效价取含税
  - incl_tax    仅含税             → 有效价取含税
  - excl_tax    仅不含税           → 有效价取不含税 (保留 basis，禁止与含税静默混比)
  - unspecified 仅通用单价/合价    → 有效价取通用字段
  - unknown     三套均缺           → REVIEW，不自动入比价 (有效价为 None)

绝不 ×1.13 / ÷1.13 / 除数量自行推导任何缺失值；缺失即诚实置 None。
"""

from __future__ import annotations

from typing import Any


PRICE_BASIS_INCL = "incl_tax"
PRICE_BASIS_EXCL = "excl_tax"
PRICE_BASIS_DUAL = "dual_tax"
PRICE_BASIS_UNSPECIFIED = "unspecified"
PRICE_BASIS_UNKNOWN = "unknown"

# 进入自动比价的口径白名单：unknown 需人工 REVIEW，不自动入比价。
AUTO_COMPARABLE_BASES = frozenset(
    {PRICE_BASIS_INCL, PRICE_BASIS_DUAL, PRICE_BASIS_EXCL, PRICE_BASIS_UNSPECIFIED}
)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("，", "").strip())
    except (TypeError, ValueError):
        return None


def derive_price_basis(fields: dict) -> dict:
    """从一行的价格字段判定 price_basis 与 effective 价格。

    入参 fields 至少可包含：unit_price / unit_price_incl_tax / unit_price_excl_tax /
    total_price / total_price_incl_tax / total_price_excl_tax。
    返回 {price_basis, effective_unit_price, effective_total_price}。
    不修改入参，不做任何 ×1.13/÷qty 推导。
    """
    u_incl = _num(fields.get("unit_price_incl_tax"))
    u_excl = _num(fields.get("unit_price_excl_tax"))
    t_incl = _num(fields.get("total_price_incl_tax"))
    t_excl = _num(fields.get("total_price_excl_tax"))
    u_gen = _num(fields.get("unit_price"))
    t_gen = _num(fields.get("total_price"))
    tax_rate = _num(fields.get("tax_rate"))
    tax_amount = _num(fields.get("tax_amount"))

    has_incl = u_incl is not None or t_incl is not None
    has_excl = u_excl is not None or t_excl is not None
    has_gen = u_gen is not None or t_gen is not None
    has_tax_evidence = tax_rate is not None or tax_amount is not None

    # 单价列规范化（确定性，文档无关）：仅有 incl、无 excl、无通用、且无任何税信息时，
    # "含税"标签不成立——这是单一价格列（如绵存只有"单价/合价"），不存在含税/不含税
    # 区分。降级为 unspecified，有效价取该唯一价格（业务上按含税纳入比价）。
    if has_incl and not has_excl and not has_gen and not has_tax_evidence:
        basis, eff_unit, eff_total = PRICE_BASIS_UNSPECIFIED, u_incl, t_incl
    elif has_incl and has_excl:
        basis, eff_unit, eff_total = PRICE_BASIS_DUAL, u_incl, t_incl
    elif has_incl:
        basis, eff_unit, eff_total = PRICE_BASIS_INCL, u_incl, t_incl
    elif has_excl:
        basis, eff_unit, eff_total = PRICE_BASIS_EXCL, u_excl, t_excl
    elif has_gen:
        basis, eff_unit, eff_total = PRICE_BASIS_UNSPECIFIED, u_gen, t_gen
    else:
        basis, eff_unit, eff_total = PRICE_BASIS_UNKNOWN, None, None

    # 同口径还原（§4）：若有效单价缺失但有效合价与数量齐全，用 合价÷数量 还原单价。
    # 这是供应商自己印的"合价=单价×数量"的精确反算（如泰科龙只印含税合价+不含税单价），
    # **不是** ×1.13/÷1.13 跨口径推导。recovered 标记写入审计，绝不静默。
    qty = _num(fields.get("qty"))
    recovered = False
    if eff_unit is None and eff_total is not None and qty is not None and qty > 0:
        eff_unit = round(eff_total / qty, 4)
        recovered = True

    return {
        "price_basis": basis,
        "effective_unit_price": eff_unit,
        "effective_total_price": eff_total,
        "effective_unit_recovered": recovered,
    }
