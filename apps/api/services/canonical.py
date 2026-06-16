"""Canonical key extraction for valve/fitting items.

Extracts structured technical attributes (valve_type, DN, PN, material,
connection) from raw name/spec text. Used for:
  - Post-processing quote items after LLM extraction
  - Building anchor canonical keys from procurement list rows
  - Hard-filter in anchor matching to prevent cross-type false positives

Design: pure functions, no I/O, no DB. Reuses standardize.py for DN/PN
normalization and synonym mapping.
"""

from __future__ import annotations

import re

from apps.api.services.standardize import standardize_name


# ── Valve type keywords (LONGER/MORE-SPECIFIC forms first) ─────────────────
_VALVE_TYPES = [
    "Y型过滤器",
    "真空破坏器",           # vacuum breaker — not a valve; must not align to valve anchors
    "流量测试",             # flow-test interface — not a valve; catches "流量测试接口控制阀门"
    "橡胶瓣止回阀",
    "电动蝶阀",
    "截止阀",
    "闸阀",
    "止回阀",
    "球阀",
    "蝶阀",
    "减压阀组",
    "减压阀",
    "疏水阀",
    "安全阀",
    "调节阀",
    "电动阀",
    "电磁阀",
    "旋塞阀",
    "过滤器",
]

# ── Material keywords (longer first to avoid prefix match) ──────────────────
_MATERIALS = [
    "不锈钢",
    "球墨铸铁",
    "铸铁",
    "黄铜",
    "铜",
    "碳钢",
    "铸钢",
    "合金钢",
    "PVC",
    "UPVC",
    "衬氟",
    "衬胶",
]

# ── Connection type keywords ─────────────────────────────────────────────────
_CONNECTIONS = ["螺纹", "法兰", "焊接", "卡箍", "卡压", "承插"]

# ── MPa → PN conversion table ────────────────────────────────────────────────
_MPA_TO_PN: dict[float, str] = {
    0.6: "PN6",
    1.0: "PN10",
    1.6: "PN16",
    2.5: "PN25",
    4.0: "PN40",
    6.4: "PN64",
}

_DN_RE = re.compile(r"DN\s*0*(\d+)", re.IGNORECASE)
_PN_RE = re.compile(r"PN\s*0*(\d+(?:\.\d+)?)", re.IGNORECASE)
_MPA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*MPA", re.IGNORECASE)


def extract_valve_canonical(
    name: str,
    spec: str,
    pressure: str = "",
    material: str = "",
) -> dict:
    """Extract structured valve attributes from raw text inputs.

    Returns a dict with keys: valve_type, dn, pn, material, connection.
    All values are str or None. Missing/unrecognizable fields are None.

    Args:
        name: material name (e.g. "截止阀")
        spec: spec string (e.g. "DN25 PN16 不锈钢")
        pressure: pressure field (e.g. "PN16" or "1.6MPa")
        material: material field text
    """
    # Concatenate all inputs, normalize via standardize_name (handles
    # Φ57→DN50, 2寸→DN50, 逆止阀→止回阀, NFKC, etc.)
    combined = " ".join(x for x in [name, spec, pressure, material] if x)
    normalized = standardize_name(combined)["standardized"]

    result: dict[str, str | None] = {
        "valve_type": None,
        "dn": None,
        "pn": None,
        "material": None,
        "connection": None,
    }

    # ── valve_type: first keyword match (longer forms scanned first) ──────
    for kw in _VALVE_TYPES:
        if kw in normalized:
            result["valve_type"] = kw
            break

    # ── dn: after standardize_name, Φ/寸/mm already converted ────────────
    m = _DN_RE.search(normalized)
    if m:
        result["dn"] = f"DN{m.group(1)}"

    # ── pn: explicit PN pattern, then MPa conversion ──────────────────────
    m = _PN_RE.search(normalized)
    if m:
        result["pn"] = f"PN{m.group(1).rstrip('.')}"
    else:
        m = _MPA_RE.search(normalized)
        if m:
            mpa_val = round(float(m.group(1)), 1)
            pn = _MPA_TO_PN.get(mpa_val)
            if pn:
                result["pn"] = pn

    # ── material: first keyword match (longer forms first) ────────────────
    for kw in _MATERIALS:
        if kw in normalized:
            result["material"] = kw
            break

    # ── connection: keyword match ──────────────────────────────────────────
    for kw in _CONNECTIONS:
        if kw in normalized:
            result["connection"] = kw
            break

    return result


# ── Valve-type families ───────────────────────────────────────────────────
# Only group valve types whose variants are genuinely *interchangeable* for
# comparison (commercial packaging differences like 组/单体/可调式), NOT types
# whose subtype carries a real engineering difference.
#
# 减压阀族 — 减压阀 / 减压阀组 / 可调式减压阀(组) / 小阻力(小型)可调式减压阀(组) …
#            all reduce to "减压阀" or "减压阀组" via extract_valve_canonical;
#            the substring fallback keeps it robust to future keyword additions.
# 止回阀 — deliberately NOT a family: 橡胶瓣 / 旋启式 / 缓闭式 / 节能消声 / 普通
#          止回阀 are different sealing mechanisms → must stay distinct.
_VALVE_FAMILY_MAP: dict[str, str] = {
    "减压阀": "减压阀族",
    "减压阀组": "减压阀族",
}
# Substring-based fallback families (string contains key → family). Keys must be
# specific enough that non-members never match (e.g. "减压阀" excludes
# "真空破坏器" / "流量测试").
_VALVE_FAMILY_SUBSTR: list[tuple[str, str]] = [
    ("减压阀", "减压阀族"),
]


def normalize_valve_family(valve_type: str | None) -> str | None:
    """Map a valve_type to its interchangeable family, else return it unchanged.

    Types without a defined family return themselves so they can only ever be
    compatible with an identical type — never accidentally merged with another.
    """
    if not valve_type:
        return valve_type
    if valve_type in _VALVE_FAMILY_MAP:
        return _VALVE_FAMILY_MAP[valve_type]
    for sub, fam in _VALVE_FAMILY_SUBSTR:
        if sub in valve_type:
            return fam
    return valve_type


def valve_type_compatible(anchor_type: str | None, quote_type: str | None) -> bool:
    """True if two valve types may align (identical, wildcard, or same family).

    - empty on either side → True (wildcard, never blocks)
    - identical → True
    - same *defined* family (not the self-fallback) → True
    - otherwise → False  (cross-type / true-subtype conflict)
    """
    if not anchor_type or not quote_type:
        return True
    if anchor_type == quote_type:
        return True
    fam_a = normalize_valve_family(anchor_type)
    fam_q = normalize_valve_family(quote_type)
    # Compatible only when both resolved to the SAME *real* family — i.e. the
    # mapping actually changed the string for at least one side (self-fallback
    # types never count as a family).
    if fam_a == fam_q and (fam_a != anchor_type or fam_q != quote_type):
        return True
    return False


def canonical_match_score(a_canon: dict, q_canon: dict) -> float:
    """Score how well two canonical key dicts match for anchor alignment.

    Returns:
        0.0  — explicit conflict (DN/PN clash, or incompatible valve_type)
        1.0  — all present fields match exactly
        0.75 — valve_type family-compatible (e.g. 减压阀组 ↔ 减压阀), DN/PN ok
        0.5  — partial / missing fields (wildcard treatment)

    None/missing values are treated as wildcards — they never trigger a hard block.
    Valve-type *families* (减压阀族) are compatible-but-not-identical → 0.75 so the
    item is recoverable while still flagged for review; true subtype/cross-type
    differences (橡胶瓣止回阀 vs 旋启式, 球阀 vs 流量测试) remain hard 0.0.
    """
    a_vt = (a_canon or {}).get("valve_type")
    q_vt = (q_canon or {}).get("valve_type")
    a_dn = (a_canon or {}).get("dn")
    q_dn = (q_canon or {}).get("dn")
    a_pn = (a_canon or {}).get("pn")
    q_pn = (q_canon or {}).get("pn")

    # Hard blocks: DN/PN are never softened
    if a_dn and q_dn and a_dn != q_dn:
        return 0.0
    if a_pn and q_pn and a_pn != q_pn:
        return 0.0

    # valve_type: exact → full; family-compatible → partial; incompatible → block
    vt_family_only = False
    # One side has valve_type, other doesn't (e.g. OCR dropped it): wildcard,
    # not a confirmed match — cap at 0.5 regardless of DN/PN agreement.
    vt_one_sided = bool(a_vt) != bool(q_vt)
    if a_vt and q_vt and a_vt != q_vt:
        if valve_type_compatible(a_vt, q_vt):
            vt_family_only = True
        else:
            return 0.0

    # At least one meaningful field on each side to count as a real match
    has_a = any(v for v in (a_vt, a_dn, a_pn) if v)
    has_q = any(v for v in (q_vt, q_dn, q_pn) if v)
    if not (has_a and has_q):
        return 0.5

    if vt_one_sided:
        return 0.5

    return 0.75 if vt_family_only else 1.0
