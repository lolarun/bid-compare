"""table_parser.py — Convert DashScope OCR HTML to structured TableGrid.

Sits between Stage-1 OCR (HTML string) and Stage-2 LLM (JSON extraction).
No external dependencies — uses Python's built-in html.parser only.

Key types
---------
TableRow   : one table row with header-keyed cells + row_type classification
TableGrid  : one <table> element on one page, with col_map to semantic slots

Usage
-----
    grids = html_to_table_grids(page_html, page_num=3)
    llm_input = grids_to_llm_json(grids)   # send to Stage-2 LLM
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from apps.api.core.enums import (
    RT_GRAND_TOTAL, RT_INVALID, RT_QUOTE_LINE,
    RT_REMARK, RT_SECTION_HEADER, RT_SUBTOTAL,
)

log = logging.getLogger(__name__)

# ── Column semantic slot detection ────────────────────────────────────────
# Order matters: more specific patterns must come before less specific ones.
# Each entry: (slot_name, [regex_patterns_for_header_text])
_COL_SLOTS: list[tuple[str, list[str]]] = [
    # seq must be first; anchored patterns avoid grabbing a "项目序号说明" remark column.
    ("seq",                 [r"^序号$", r"^序$", r"^编号$", r"^项次$", r"^序\s*号$"]),
    # name: 材料(设备)/材料(设备)名称 are common procurement form headers for item names
    ("name",                [r"材料名称", r"物料名称", r"品名", r"货品名称", r"名称$",
                              r"^材料[（(]"]),
    ("spec",                [r"规格型号", r"规格"]),
    ("model",               [r"^型号$", r"^品牌型号$"]),
    ("material_type",       [r"材质", r"牌号"]),
    ("unit",                [r"^单位$", r"计量单位", r"单位$"]),
    # qty: 数量(个)/数量(套)/数量（件）are common suffixed forms
    ("qty",                 [r"数量[（(]", r"数量$", r"工程量"]),
    # unit_price_excl_tax BEFORE unit_price (same lookbehind logic as tabular_ingestion)
    ("unit_price_excl_tax", [r"不含税单价", r"裸价"]),
    # unit_price: 单价(元)/单价（元）are common suffixed forms in valve/equipment tables
    ("unit_price",          [r"(?<!不)含税单价", r"^单价$", r"单价[（(]元[）)]", r"单价$"]),
    # total_price: 含税合价/含税合计 BEFORE generic 合价 to avoid shadowing the incl-tax column;
    #              合价(元)/合计(元) with yuan suffix; 合计$ anchored to avoid matching 价税合计 twice
    ("total_price",         [r"价税合计", r"含税[合总][价计]", r"[合总][价计][（(]元[）)]",
                              r"合价", r"合计$", r"总价", r"金额$"]),
    ("brand",               [r"品牌", r"厂家", r"制造商"]),
    ("remark",              [r"备注", r"说明$"]),
]

_GRAND_TOTAL_KEYWORDS = re.compile(r"价税合计|总计|合计金额|投标总价|合计|含税总计|含税合计|详见投标清单")
_SUBTOTAL_KEYWORDS = re.compile(r"小计")
_HEADER_KEYWORDS = re.compile(
    r"序号|材料名称|品名|规格型号|单位|数量|单价|合价|备注|名称"
)
_NUMBER_RE = re.compile(r"^[\d,，.\-\s]+$")


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class TableRow:
    row_index: int          # 0-based index in original HTML table rows
    row_type: str           # quote_line / subtotal / grand_total / header / empty / note
    cells: dict[str, str]   # header_text → cell_text
    raw_values: list[str] = None   # full positional cell list (includes tail beyond header)

    def __repr__(self) -> str:
        preview = {k: v for k, v in list(self.cells.items())[:3]}
        return f"TableRow(idx={self.row_index}, type={self.row_type}, cells={preview})"


@dataclass
class TableGrid:
    page: int               # 1-based page number
    table_index: int        # 0-based table index within the page HTML
    header: list[str]       # original header cell texts
    col_map: dict[str, str] # header_text → semantic slot name
    rows: list[TableRow]
    def to_llm_dict(self) -> dict:
        """JSON-serialisable dict for LLM prompt (excludes empty/header rows)."""
        return {
            "page": self.page,
            "table_index": self.table_index,
            "rows": [
                {
                    "row_index": r.row_index,
                    "row_type": r.row_type,
                    "cells": r.cells,
                }
                for r in self.rows
                if r.row_type not in (RT_INVALID, RT_SECTION_HEADER)
            ],
        }

    def quote_line_count(self) -> int:
        return sum(1 for r in self.rows if r.row_type == "quote_line")


# ── HTML table parser (standard library) ─────────────────────────────────

class _TableHTMLParser(HTMLParser):
    """SAX-style parser collecting all <table> elements from HTML string."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Collected tables: list[ list[row] ] where row = list[cell_dict]
        self.tables: list[list[list[dict]]] = []
        self._current_table: list[list[dict]] | None = None
        self._current_row: list[dict] | None = None
        self._in_cell: bool = False
        self._cell_text: str = ""
        self._cell_colspan: int = 1
        self._cell_rowspan: int = 1

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr_dict = dict(attrs)
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._in_cell = True
            self._cell_text = ""
            try:
                self._cell_colspan = int(attr_dict.get("colspan") or 1)
            except (ValueError, TypeError):
                self._cell_colspan = 1
            try:
                self._cell_rowspan = int(attr_dict.get("rowspan") or 1)
            except (ValueError, TypeError):
                self._cell_rowspan = 1
        elif tag == "br" and self._in_cell:
            self._cell_text += " "

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._current_table is not None:
            self.tables.append(self._current_table)
            self._current_table = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if self._current_row:  # skip empty <tr></tr>
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag in ("td", "th") and self._in_cell and self._current_row is not None:
            self._current_row.append({
                "text": self._cell_text.strip(),
                "colspan": self._cell_colspan,
                "rowspan": self._cell_rowspan,
            })
            self._in_cell = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text += data


# ── rowspan/colspan expansion ─────────────────────────────────────────────

def _expand_table_rows(raw_rows: list[list[dict]]) -> list[list[str]]:
    """Expand rowspan/colspan to a flat 2D list[list[str]]."""
    if not raw_rows:
        return []

    # Estimate column count: max sum of colspans in any single row
    col_count = max(
        (sum(max(1, c.get("colspan", 1)) for c in row) for row in raw_rows),
        default=0,
    )
    if col_count == 0:
        return []

    grid: list[list[str | None]] = []
    # span_carry[future_row_idx][col_idx] = text from above rowspan
    span_carry: dict[int, dict[int, str]] = {}

    for r_idx, row in enumerate(raw_rows):
        row_data: list[str | None] = [None] * col_count

        # Apply carried cells from rowspan above
        for col_idx, text in (span_carry.get(r_idx) or {}).items():
            if col_idx < col_count:
                row_data[col_idx] = text

        # Place cells from this row
        c_pos = 0
        for cell in row:
            # Advance past positions already filled by rowspan carries
            while c_pos < col_count and row_data[c_pos] is not None:
                c_pos += 1
            if c_pos >= col_count:
                break

            text = cell["text"]
            colspan = max(1, cell.get("colspan", 1))
            rowspan = max(1, cell.get("rowspan", 1))

            for dc in range(colspan):
                if c_pos + dc < col_count:
                    row_data[c_pos + dc] = text

            if rowspan > 1:
                for dr in range(1, rowspan):
                    target = r_idx + dr
                    if target not in span_carry:
                        span_carry[target] = {}
                    for dc in range(colspan):
                        span_carry[target][c_pos + dc] = text

            c_pos += colspan

        grid.append([v if v is not None else "" for v in row_data])

    return grid


# ── header detection & column mapping ────────────────────────────────────

def _detect_header(rows_2d: list[list[str]]) -> tuple[list[str] | None, int]:
    """Scan first 5 rows for a header row. Returns (header_texts, data_start_row)."""
    for i, row in enumerate(rows_2d[:5]):
        combined = " ".join(row)
        non_empty = [c for c in row if c.strip()]
        if len(non_empty) >= 3 and _HEADER_KEYWORDS.search(combined):
            return [c.strip() for c in row], i + 1
    return None, 0


def _map_columns(header: list[str]) -> dict[str, str]:
    """Map header texts to semantic slot names. Each slot claimed at most once."""
    col_map: dict[str, str] = {}
    claimed_slots: set[str] = set()

    for slot, patterns in _COL_SLOTS:
        if slot in claimed_slots:
            continue
        for h_text in header:
            if h_text in col_map:
                continue
            for pat in patterns:
                if re.search(pat, h_text, re.IGNORECASE):
                    col_map[h_text] = slot
                    claimed_slots.add(slot)
                    break
            if slot in claimed_slots:
                break

    return col_map


# ── row type classification ───────────────────────────────────────────────

def _classify_row(cells: dict[str, str]) -> str:
    """Classify a data row by its content pattern."""
    values = list(cells.values())
    non_empty = [v for v in values if v.strip()]

    if not non_empty:
        return RT_INVALID

    # Split into text cells (non-numeric) and numeric cells
    text_cells = [
        v for v in non_empty
        if not _NUMBER_RE.match(re.sub(r"[¥￥元,，\s]", "", v))
    ]
    combined = " ".join(non_empty)

    # grand_total: keyword appears in a text cell AND most columns are empty
    empty_ratio = (len(values) - len(non_empty)) / max(len(values), 1)
    if text_cells and any(_GRAND_TOTAL_KEYWORDS.search(v) for v in text_cells):
        if empty_ratio >= 0.3:
            return RT_GRAND_TOTAL

    # subtotal
    if _SUBTOTAL_KEYWORDS.search(combined):
        return RT_SUBTOTAL

    # Repeated header row (e.g. after page break)
    if _HEADER_KEYWORDS.search(combined) and len(non_empty) >= 3:
        # If price columns are not numeric, likely a repeated header
        price_vals = [
            v for k, v in cells.items()
            if any(kw in k for kw in ("价", "金额", "合价"))
        ]
        if price_vals and not any(_NUMBER_RE.match(v.replace(",", "")) for v in price_vals if v):
            return RT_SECTION_HEADER

    # Single non-empty text cell, no numeric price — remark/annotation row
    if len(non_empty) == 1:
        return RT_REMARK

    return RT_QUOTE_LINE


# ── public API ────────────────────────────────────────────────────────────

def html_to_table_grids(html: str, page_num: int,
                        inherited_header: list[str] | None = None) -> list[TableGrid]:
    """Parse OCR HTML string into a list of TableGrid objects.

    Returns an empty list if parsing fails or no valid tables are found.
    Designed for DashScope table_parsing HTML output but handles generic HTML.

    ``inherited_header``: optional column header list from the immediately preceding
    page.  When a table has no detectable header row (continuation page), and the
    table has the *exact* same number of columns as the inherited header, the
    inherited header is used in place of a missing header row.  This enables
    deterministic extraction from headerless continuation pages without LLM
    re-transcription.
    """
    if not html or not html.strip():
        return []

    parser = _TableHTMLParser()
    try:
        parser.feed(html)
    except Exception as e:
        log.warning("HTML parse error on page %d: %s", page_num, e)
        return []

    grids: list[TableGrid] = []

    for t_idx, raw_rows in enumerate(parser.tables):
        if len(raw_rows) < 2:
            continue

        try:
            expanded = _expand_table_rows(raw_rows)
        except Exception as e:
            log.warning("Table %d expand error on page %d: %s", t_idx, page_num, e)
            continue

        if not expanded:
            continue

        header, data_start = _detect_header(expanded)
        if not header or len(header) < 3:
            # Try inherited header when column count exactly matches (cross-page continuation)
            if (inherited_header and len(inherited_header) >= 3
                    and len(expanded[0]) == len(inherited_header)):
                header = list(inherited_header)
                data_start = 0  # all rows are data rows — no header row on this page
                log.info("Page %d table %d: using inherited header (%d cols)",
                         page_num, t_idx, len(header))
            else:
                continue

        col_map = _map_columns(header)

        table_rows: list[TableRow] = []
        for r_idx, row_cells_flat in enumerate(expanded):
            cells = dict(zip(header, row_cells_flat))
            row_type = RT_SECTION_HEADER if r_idx < data_start else _classify_row(cells)
            table_rows.append(TableRow(
                row_index=r_idx,
                row_type=row_type,
                cells={k: v for k, v in cells.items() if k},  # skip empty header keys
                raw_values=list(row_cells_flat),               # preserve tail beyond header
            ))

        grid = TableGrid(
            page=page_num,
            table_index=t_idx,
            header=header,
            col_map=col_map,
            rows=table_rows,
        )
        log.debug(
            "Page %d table %d: %d header cols, %d quote_lines, %d total rows",
            page_num, t_idx, len(header), grid.quote_line_count(), len(table_rows),
        )
        grids.append(grid)

    return grids


def grids_to_llm_json(grids: list[TableGrid]) -> str:
    """Serialise TableGrid list to compact JSON string for LLM prompt input."""
    return json.dumps(
        {"tables": [g.to_llm_dict() for g in grids]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
