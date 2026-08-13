"""tabular_ingestion.py — Deterministic CSV / Excel quote extractor.

Entry point: extract_quote_tabular(file_path, ctx) -> dict

Returns the same result shape as pipeline.ExtractionPipeline._postprocess_quote so
that document_ingestion.run_job can write job.result and let the shared downstream
(batch-confirm → anchor-match → 90-row matrix) proceed unchanged.

Design constraints (from approved plan):
- One file = one supplier.  Multi-supplier column guard hard-rejects the file.
- bid_total MUST come from an independent totals row in the file, NEVER from
  summing line items (that would be a self-referential tautology).
- Column detection uses this module's own _TABULAR_COLUMN_PATTERNS instead of
  import_service._COLUMN_PATTERNS: the import_service pattern maps 价税合计 to
  the "price" (unit-price) slot which is wrong for report tables where 价税合计
  is almost always the grand-total row, not a per-item unit price.
- 0 items after parsing → ValueError (hard fail; prevents silent empty uploads).
- source_ref is preserved until batch-confirm only.  It is NOT persisted in the
  Quote model.  If future LLM Top-K judging needs row-level evidence, add a
  Quote.source_ref_json column at that point.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ─── tabular-specific column patterns ────────────────────────────────────────
# Separate from import_service._COLUMN_PATTERNS:
# - "unit_price": only genuine per-item unit-price columns (NOT 价税合计)
# - "total_price": per-row extended amount (合价/金额/价税合计 when not a footer)
# - Longest/most-specific patterns are listed first; first match wins.
#
# Note: 含税单价 < 单价 priority ensures more-specific pattern wins when both columns
# exist (e.g. "含税单价" and "不含税单价" in the same file).

# Detection order is SIGNIFICANT: more-specific price roles must be detected
# before the generic unit_price, and each column is claimed at most once (see
# _detect_tabular_columns). This prevents "不含税单价" from being grabbed by
# unit_price (it contains the substring "含税单价"). Defense-in-depth: incl-tax
# patterns carry a (?<!不) negative lookbehind so they never match a 不含税 column
# even if it were somehow still a candidate.
_TABULAR_COLUMN_PATTERNS: dict[str, list[str]] = {
    "name":           [r"材料名称", r"设备名称", r"物料名称", r"名称", r"品名"],
    "spec":           [r"规格型号", r"规格", r"型号"],
    "brand":          [r"品牌", r"厂家", r"制造商"],
    "supplier":       [r"供应商", r"投标单位", r"报价单位", r"厂商名称"],
    "material_type":  [r"材质", r"牌号"],
    "unit":           [r"计量单位", r"单位"],
    "quantity":       [r"数量", r"工程量", r"数 量"],
    # total_price: per-row amount column (合价, 金额, 含税合价, etc.). Detected
    # BEFORE unit prices so a totals column never falls through to unit_price.
    # Note: 价税合计 here = per-row total column header, NOT the grand-total row
    # (grand-total ROW detection is handled by _row_is_total() separately).
    "total_price":    [r"含税合价", r"含税.*合价", r"合价.*含税", r"价税合计", r"合价", r"金额", r"总价(?!.*合计)"],
    # unit_price_excl_tax detected BEFORE unit_price and claims its column first.
    "unit_price_excl_tax": [r"不含税单价", r"不含税.*单价", r"单价.*不含税", r"不含税价"],
    # unit_price: most generic, detected LAST; (?<!不) guards the incl-tax forms.
    "unit_price":     [r"(?<!不)含税单价", r"(?<!不)含税.*单价", r"单价.*含税", r"(?<!不)含税价(?!合)", r"(?<!合)单价(?!.*合)"],
    "remark":         [r"备注", r"说明"],
}


def _detect_tabular_columns(columns: list[str]) -> dict[str, str | None]:
    """Detect column roles using _TABULAR_COLUMN_PATTERNS.

    Iterates roles in dict order (significant — see comment above); within a
    role, first matching unclaimed column wins. A column claimed by an earlier
    role (e.g. unit_price_excl_tax) is excluded from later roles (unit_price),
    so "不含税单价" can never be mis-assigned to unit_price.
    """
    result: dict[str, str | None] = {}
    claimed: set[str] = set()
    for field, patterns in _TABULAR_COLUMN_PATTERNS.items():
        result[field] = None
        for pattern in patterns:
            for col in columns:
                if col in claimed:
                    continue
                if re.search(pattern, str(col)):
                    result[field] = col
                    claimed.add(col)
                    break
            if result[field]:
                break
    return result


def _get_cell(row, col_map: dict, field: str) -> str | None:
    """Read a cell value from a pandas row via col_map; return stripped str or None."""
    import pandas as pd
    col = col_map.get(field)
    if not col:
        return None
    val = row.get(col)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s and s != "nan" else None


# ─── better number parser ─────────────────────────────────────────────────────

def _parse_number(s: str | None) -> float | None:
    """Parse a numeric string from Chinese supplier tables.

    Handles:
    - Thousand separators: "1,234.56" → 1234.56
    - Chinese full-width commas/periods: "１，２３４．５６"
    - Leading currency symbols: "¥1,234.56", "￥1,234.56"
    - Trailing units: "1,234.56元", "1000个"
    - Empty / NaN strings → None
    - Negative values preserved

    Does NOT handle European "1.234,56" (rare in Chinese supply chain).
    """
    if s is None or s == "":
        return None
    # Full-width normalisation
    s = s.translate(str.maketrans("０１２３４５６７８９．，￥", "0123456789.,¥"))
    # Strip currency symbols and whitespace
    s = s.strip().lstrip("¥$€£").strip()
    # Remove thousand-separator commas (e.g. "1,234.56")
    # Safe only when comma is followed by exactly 3 digits before next comma/dot/end
    s = re.sub(r",(\d{3})(?=[,\d.]|$)", r"\1", s)
    # Also handle plain comma separators without strict positional check
    s = s.replace(",", "")
    # Strip trailing non-numeric characters (units like 元/个/套)
    s = re.sub(r"[^\d.\-].*$", "", s)
    if not s or s in {".", "-", "-."}:
        return None
    try:
        v = float(s)
        return v if v == v else None  # guard NaN
    except ValueError:
        return None


def _to_positive(s: str | None) -> float | None:
    """Parse number and return None if ≤ 0."""
    v = _parse_number(s)
    return v if v is not None and v > 0 else None


# ─── footer / totals markers ─────────────────────────────────────────────────
# Used to detect a "grand total" row that supplies the checksum-safe bid_total.
# Rows matching these markers are EXCLUDED from items.

_TAX_INCL_MARKERS = ("价税合计", "含税合价", "含税合计", "含税总价", "含税小计")
_TAX_EXCL_MARKERS = ("不含税合计", "不含税总价", "不含税小计")
_GENERIC_TOTAL_MARKERS = ("合计", "总计", "总价", "总报价", "小计")


def _detect_total_row_basis(cell_text: str) -> str | None:
    """Return bid_total_basis if cell_text looks like a totals-row marker, else None."""
    for m in _TAX_INCL_MARKERS:
        if m in cell_text:
            return "tax_included"
    for m in _TAX_EXCL_MARKERS:
        if m in cell_text:
            return "tax_excluded"
    for m in _GENERIC_TOTAL_MARKERS:
        if m in cell_text:
            return "unknown"
    return None


def _row_is_total(row_cells: list[str]) -> str | None:
    """If any cell in the row contains a totals marker, return the basis; else None."""
    for cell in row_cells:
        basis = _detect_total_row_basis(cell)
        if basis is not None:
            return basis
    return None


# ─── CSV loading with encoding fallback ──────────────────────────────────────

def _load_dataframe(file_path: str):
    """Load an Excel or CSV file into a pandas DataFrame (dtype=str, header=0).

    CSV: tries utf-8-sig first, falls back to gbk (common for Chinese suppliers).
    Raises ValueError with a user-friendly message on parse failure.
    """
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pandas is required for tabular ingestion") from e

    ext = Path(file_path).suffix.lower()
    if ext in {".xlsx", ".xls"}:
        try:
            return pd.read_excel(file_path, header=0, dtype=str)
        except Exception as e:
            raise ValueError(f"Excel 文件解析失败: {e}") from e
    else:
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                df = pd.read_csv(file_path, header=0, dtype=str, encoding=enc)
                return df
            except UnicodeDecodeError:
                continue
            except Exception as e:
                raise ValueError(f"CSV 文件解析失败: {e}") from e
        raise ValueError("CSV 文件编码无法识别，请另存为 UTF-8 格式后重试")


# ─── main extractor ───────────────────────────────────────────────────────────

def extract_quote_tabular(file_path: str, ctx: dict) -> dict:
    """Extract quote items from an Excel / CSV file.

    Args:
        file_path: absolute path to the file on disk.
        ctx: ExtractionJob.context dict; may contain supplier_name, project_id, etc.

    Returns:
        dict with keys: supplier_name, quote_date, items, context, _doc_meta
        Shape is identical to pipeline._postprocess_quote output so that
        document_ingestion.run_job can write it to job.result unchanged.

    Raises:
        ValueError: for multi-supplier files, unreadable files, missing name
                    column, or zero valid items after parsing.  Zero items is
                    treated as a hard failure to prevent silent empty uploads.
    """
    import pandas as pd
    from apps.api.intelligence.quote_fact import quote_fact_from_row, apply_arithmetic_validation

    # ── 1. Load ───────────────────────────────────────────────────────────────
    df = _load_dataframe(file_path)
    if df.empty:
        raise ValueError("文件为空，未找到任何数据行")

    # ── 2. Column detection ───────────────────────────────────────────────────
    col_map = _detect_tabular_columns(df.columns.tolist())

    if not col_map.get("name"):
        raise ValueError(
            f"未识别到物料名称列（尝试列名：名称/品名/材料名称/设备名称）。"
            f"实际列名：{df.columns.tolist()}"
        )

    # ── 3. Multi-supplier guard ───────────────────────────────────────────────
    supplier_col = col_map.get("supplier")
    if supplier_col:
        unique_suppliers = (
            df[supplier_col]
            .dropna()
            .astype(str)
            .str.strip()
            .replace("nan", pd.NA)
            .dropna()
            .unique()
        )
        unique_suppliers = [s for s in unique_suppliers if s]
        if len(unique_suppliers) > 1:
            raise ValueError(
                f"检测到多供应商列 ({len(unique_suppliers)} 家)，"
                "请按供应商拆分后单独上传"
            )

    # ── 4. Supplier name resolution ───────────────────────────────────────────
    # Priority: (a) explicit ctx key → (b) first non-empty supplier column cell → (c) ""
    supplier_name: str = (ctx.get("supplier_name") or "").strip()
    if not supplier_name and supplier_col:
        first_sup = _get_cell(df.iloc[0], col_map, "supplier")
        if first_sup:
            supplier_name = first_sup

    # ── 5. Row iteration ──────────────────────────────────────────────────────
    items: list[dict] = []
    bid_total: float | None = None
    bid_total_basis: str = "unknown"
    found_total_row: bool = False

    for idx, row in df.iterrows():
        # Stringify all non-empty cells for totals-row marker scan
        row_cells = [
            str(v).strip()
            for v in row.values
            if v is not None and str(v).strip() not in ("", "nan")
        ]

        # ── totals-row check ──────────────────────────────────────────────────
        basis = _row_is_total(row_cells)
        if basis is not None:
            # Try to extract bid_total from the total_price column (preferred)
            # or fall back to the unit_price column (some files put grand total there).
            if not found_total_row:
                raw_total = _get_cell(row, col_map, "total_price") or _get_cell(row, col_map, "unit_price")
                if raw_total:
                    v = _parse_number(raw_total)
                    if v is not None and v > 0:
                        bid_total = v
                        bid_total_basis = basis
                        found_total_row = True
                        log.debug(
                            "tabular_ingestion: totals row idx=%s  bid_total=%s basis=%s",
                            idx, bid_total, bid_total_basis,
                        )
            # Exclude from items regardless
            continue

        # ── name check ────────────────────────────────────────────────────────
        raw_name = _get_cell(row, col_map, "name")
        if not raw_name:
            continue

        # ── numeric fields ────────────────────────────────────────────────────
        qty        = _to_positive(_get_cell(row, col_map, "quantity"))
        unit_price = _to_positive(_get_cell(row, col_map, "unit_price"))
        unit_price_excl = _to_positive(_get_cell(row, col_map, "unit_price_excl_tax"))

        # per-row extended amount (合价): prefer explicit column, derive if absent
        raw_total_price = _get_cell(row, col_map, "total_price")
        total_price = _to_positive(raw_total_price)
        # If no total_price column, QuoteFact.__post_init__ derives qty×unit_price

        spec   = _get_cell(row, col_map, "spec")   or ""
        brand  = _get_cell(row, col_map, "brand")  or ""
        unit   = _get_cell(row, col_map, "unit")   or ""
        material_type = _get_cell(row, col_map, "material_type") or ""
        remark = _get_cell(row, col_map, "remark") or ""

        # ── construct QuoteFact ───────────────────────────────────────────────
        fact = quote_fact_from_row(
            material=raw_name.strip(),
            spec=spec.strip(),
            brand=brand.strip(),
            unit=unit.strip(),
            qty=qty,
            unit_price=unit_price,
            unit_price_excl_tax=unit_price_excl,
            total_price=total_price,
            material_type=material_type.strip(),
            remark=remark.strip(),
            source_ref={"sheet": _sheet_name(file_path), "row": int(idx) + 2},
        )
        items.append(fact.to_item_dict())

    # ── 6. Hard fail on 0 items ───────────────────────────────────────────────
    # Prevents silent empty uploads that would pass batch-confirm but produce
    # zero rows in the bid matrix with no visible error.
    if not items:
        raise ValueError(
            "未解析到有效报价行（名称列已识别但所有行为空或被过滤）。"
            "请检查文件格式是否与列检测规则匹配。"
        )

    # ── 7. Arithmetic validation (same rule as PDF path) ─────────────────────
    apply_arithmetic_validation(items)

    # ── 8. Assemble result (same shape as _postprocess_quote) ─────────────────
    doc_meta: dict[str, Any] = {
        "bid_total": bid_total,
        "bid_total_basis": bid_total_basis,
        "supplier_name": supplier_name,
    }

    log.info(
        "tabular_ingestion: %s → %d items, bid_total=%s (%s)",
        Path(file_path).name, len(items), bid_total, bid_total_basis,
    )

    return {
        "supplier_name": supplier_name,
        "quote_date": "",
        "items": items,
        "context": ctx,
        "_doc_meta": doc_meta,
    }


# ─── private helpers ──────────────────────────────────────────────────────────

def _sheet_name(file_path: str) -> str:
    """Return first sheet name for xlsx, or filename stem for csv."""
    ext = Path(file_path).suffix.lower()
    if ext in {".xlsx", ".xls"}:
        try:
            import pandas as pd
            xl = pd.ExcelFile(file_path)
            return xl.sheet_names[0] if xl.sheet_names else "Sheet1"
        except Exception:
            pass
    return Path(file_path).stem
