"""品牌硬信号：基于招标文件第13页的「业主品牌要求」+「供应商参与品牌」做匹配校验。

设计要点（见 design 计划「品牌硬信号」节）：
- 别名集合用中英文（KITZ↔开滋 等），双向包含匹配，容忍 OCR/书写差异。
- 冲突只「降级 pending」不 reject —— 别名表可能不全，宁可保守。
- 报价品牌为空 → 无信号（unknown），不降级。
"""

from __future__ import annotations


def _norm(s: str) -> str:
    """归一化品牌串：小写、去空白/标点，便于包含匹配。"""
    if not s:
        return ""
    out = []
    for ch in str(s):
        if ch.isalnum() or "一" <= ch <= "鿿":
            out.append(ch.lower())
    return "".join(out)


def build_brand_context(
    brand_requirement: list | None,
    supplier_brand_map: list | None,
) -> tuple[set[str], dict[int, set[str]]]:
    """从 session 字段构建 (allowed_aliases, supplier_expected_aliases)。

    allowed_aliases: 所有业主允许品牌的归一别名集合（含中英文）。
    supplier_expected_aliases: {supplier_id: {该供应商参与品牌的归一别名}}。
    """
    allowed: set[str] = set()
    # 中文品牌 → 该品牌的全部别名（用于把供应商参与品牌(中文)扩展出英文别名）
    cn_to_aliases: dict[str, set[str]] = {}
    for b in (brand_requirement or []):
        if not isinstance(b, dict):
            continue
        en = _norm(b.get("brand_en") or "")
        cn = _norm(b.get("brand_cn") or "")
        aliases = {x for x in (en, cn) if x}
        allowed |= aliases
        if cn:
            cn_to_aliases[cn] = aliases

    supplier_expected: dict[int, set[str]] = {}
    for sb in (supplier_brand_map or []):
        if not isinstance(sb, dict):
            continue
        sid = sb.get("supplier_id")
        if sid is None:
            continue
        brand_cn = _norm(sb.get("brand") or "")
        if not brand_cn:
            continue
        # 该供应商参与品牌的别名 = 自身 + 命中的业主品牌别名集合
        aliases = {brand_cn} | cn_to_aliases.get(brand_cn, set())
        supplier_expected[int(sid)] = aliases
    return allowed, supplier_expected


def _hit(quote_brand_norm: str, aliases: set[str]) -> bool:
    """归一报价品牌与别名集合双向包含匹配。"""
    return any(a and (a in quote_brand_norm or quote_brand_norm in a) for a in aliases)


def check_brand(
    quote_brand: str,
    allowed_aliases: set[str],
    expected_aliases: set[str] | None,
) -> str:
    """返回 'match' | 'allowed' | 'conflict' | 'unknown'。

    - unknown: 报价无品牌，或未配置品牌要求（无信号）。
    - match: 命中该供应商应投品牌（强证据）。
    - allowed: 在业主允许范围内，但非该供应商登记品牌（弱）。
    - conflict: 报价有品牌但不在业主允许范围（→ 调用方降级 pending）。
    """
    qn = _norm(quote_brand)
    if not qn or not allowed_aliases:
        return "unknown"
    if expected_aliases and _hit(qn, expected_aliases):
        return "match"
    if _hit(qn, allowed_aliases):
        return "allowed"
    return "conflict"
