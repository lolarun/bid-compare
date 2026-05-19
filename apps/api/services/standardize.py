"""Material name standardization service.

Normalizes spec formats, synonyms, and common variations found in
construction MEP (mechanical/electrical/plumbing) bidding documents.
"""

import re

# ─── Spec format normalization ───────────────────────────────────────────────

# DN aliases: "100mm", "Φ108", "4寸", "4\"" → "DN100"
_DN_INCH_MAP = {
    "1/2": 15, "3/4": 20, "1": 25, "1.25": 32, "1.5": 40, "2": 50,
    "2.5": 65, "3": 80, "4": 100, "5": 125, "6": 150, "8": 200,
    "10": 250, "12": 300,
}

_DN_OD_MAP = {  # OD (外径) → DN
    15: 15, 18: 15, 20: 15, 22: 20, 25: 20, 27: 20, 32: 25, 34: 25,
    42: 32, 48: 40, 57: 50, 60: 50, 76: 65, 89: 80, 108: 100, 114: 100,
    133: 125, 140: 125, 159: 150, 168: 150, 219: 200, 273: 250, 325: 300,
    377: 350, 426: 400, 480: 450, 530: 500, 630: 600,
}


def _normalize_dn(text: str) -> tuple[str, list[str]]:
    """Normalize DN specifications."""
    changes = []

    # "Φ108" or "φ108" → DN100
    m = re.search(r'[ΦφΦ]\s*(\d+)', text)
    if m:
        od = int(m.group(1))
        dn = _DN_OD_MAP.get(od)
        if dn:
            text = text[:m.start()] + f"DN{dn}" + text[m.end():]
            changes.append(f"Φ{od} → DN{dn}")

    # "4寸" → DN100
    m = re.search(r'(\d+(?:\.\d+)?)\s*寸', text)
    if m:
        inch_str = m.group(1)
        dn = _DN_INCH_MAP.get(inch_str)
        if dn:
            text = text[:m.start()] + f"DN{dn}" + text[m.end():]
            changes.append(f"{inch_str}寸 → DN{dn}")

    # "100mm" without DN prefix → DN100 (only for pipe-like contexts)
    m = re.search(r'(?<![×xX*\d])(\d+)\s*mm(?!\s*[×xX*])', text)
    if m and not re.search(r'DN\d', text):
        val = int(m.group(1))
        if val in (15, 20, 25, 32, 40, 50, 65, 80, 100, 125, 150, 200, 250, 300, 350, 400, 450, 500, 600):
            text = text[:m.start()] + f"DN{val}" + text[m.end():]
            changes.append(f"{val}mm → DN{val}")

    return text, changes


def _normalize_dimensions(text: str) -> tuple[str, list[str]]:
    """Normalize dimension separators: 300*150 → 300×150."""
    changes = []
    pattern = r'(\d+)\s*[*xX]\s*(\d+)'
    if re.search(pattern, text):
        new_text = re.sub(pattern, r'\1×\2', text)
        if new_text != text:
            changes.append("尺寸分隔符 → ×")
            text = new_text
    return text, changes


# ─── Name synonym normalization ──────────────────────────────────────────────

_SYNONYMS = [
    # Surface treatment
    (r'热镀锌|热浸锌', '热浸镀锌'),
    (r'冷镀锌', '电镀锌'),
    # Bridge types
    (r'电缆桥架|线缆桥架', '桥架'),
    (r'槽式托盘', '槽式'),
    (r'托盘型', '托盘式'),
    # Valve types
    (r'蝶型阀', '蝶阀'),
    (r'闸板阀', '闸阀'),
    (r'逆止阀|单向阀', '止回阀'),
    # Pipe
    (r'不锈钢无缝管', '不锈钢管'),
    # Pump
    (r'排污泵|污水泵', '潜水泵'),
    # HVAC
    (r'风机盘管机组', '风机盘管'),
    (r'风盘', '风机盘管'),
]


def _normalize_synonyms(text: str) -> tuple[str, list[str]]:
    """Replace common synonyms with standard names."""
    changes = []
    for pattern, replacement in _SYNONYMS:
        m = re.search(pattern, text)
        if m:
            original_match = m.group(0)
            text = re.sub(pattern, replacement, text)
            if original_match != replacement:
                changes.append(f"{original_match} → {replacement}")
    return text, changes


# ─── Whitespace normalization ────────────────────────────────────────────────

def _normalize_whitespace(text: str) -> tuple[str, list[str]]:
    """Normalize whitespace: full-width spaces, multiple spaces, trim."""
    changes = []
    if '　' in text:
        text = text.replace('　', ' ')
        changes.append("全角空格 → 半角")
    if '  ' in text:
        text = re.sub(r'\s+', ' ', text)
        changes.append("去除多余空格")
    text = text.strip()
    return text, changes


# ─── Public API ──────────────────────────────────────────────────────────────

def standardize_name(text: str, category: str | None = None) -> dict:
    """Standardize a material name/spec string.

    Returns: {"original": ..., "standardized": ..., "changes": [...]}
    """
    if not text or not text.strip():
        return {"original": text, "standardized": text, "changes": []}

    original = text
    all_changes: list[str] = []

    text, changes = _normalize_whitespace(text)
    all_changes.extend(changes)

    text, changes = _normalize_dimensions(text)
    all_changes.extend(changes)

    text, changes = _normalize_dn(text)
    all_changes.extend(changes)

    text, changes = _normalize_synonyms(text)
    all_changes.extend(changes)

    return {
        "original": original,
        "standardized": text,
        "changes": all_changes,
    }
