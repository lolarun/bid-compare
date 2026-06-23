# 08 — Tender PDF Recognition Generalization Design

> **Status — audited 2026-06-23.** Design draft, largely NOT yet implemented. The category-aware page scoring, `_detect_category`, per-category prompt/schema maps, and the `category_override` / `field_map_override` parameters described below do not exist in the code; `_score_page` still hardcodes valve keywords and both anchor builders call `extract_valve_canonical` unconditionally. The one piece that *did* land separately (via design 07) is content-based category detection in `apps/api/services/category_classify.py`, which `extract_bidlist` now uses to populate `detected_category`.
> _Originally written as a pre-implementation design draft. English translation of the Chinese original; now the authoritative version._

> Status: design draft, to be implemented after discussion

## Background and problem

The current `apps/api/services/tender_pdf.py` has an obvious **valve / water-supply-and-drainage specialization** problem:

| Symptom | Specific code |
|---|---|
| Page scoring hardcodes valve keywords | `_score_page`: `工作压力` / `阀体` / `密封圈` / `DN\d` etc. |
| Error message hardcodes valve fields | `raise ValueError("含 序号/项目名称/工作压力/材质")` |
| Anchor construction calls valve canonicalization | `extract_valve_canonical(...)` |
| Material field assumes five fixed columns | `阀体/阀芯/阀板/阀杆/密封圈` |
| Extraction prompt only describes the valve format | `TENDER_BIDLIST_PROMPT` |

This means that uploading a tender document of another category (cable tray, panel, pipe, pump, HVAC diffuser/damper) will:
- Score almost 0 on every page → location fails
- If it somehow does enter extraction, the LLM is misled by the valve prompt → field misalignment

> _(corrected 2026-06-23: the second `_score_page`-related claim is partly stale. The code does not `raise ValueError("含 序号/项目名称/工作压力/材质")`; that message lives in `parse_tender_xlsx` in `tender_list.py` as `"找不到可识别的表头(第一版仅支持规范表头;序号/名称/数量缺失)"`. The valve-keyword hardcoding in `_score_page` and the unconditional `extract_valve_canonical` calls are still present and accurate as described.)_

## Goal

Be able to correctly handle **at least 6 categories** of tender list formats, with precision per category no lower than the current valve level (89 rows, seq 0 missing, source_ref 100%).

---

## 1. Generalized page scoring

### Current state

```python
def _score_page(html):
    if "工作压力" in html: bs += 0.3   # valve-specific
    if any(kw in html for kw in ("阀体", "密封圈")): bs += 0.2  # valve-specific
    if _DN_RE.search(html): bs += 0.2   # valve-specific
```

### Goal

Base score (category-agnostic) + category-weighted score (used for category_guess, not for bidlist gating):

```
bidlist_base_score  = Σ generic-signal weights
  generic signals:
    seq column      +0.35   "序号" | "编号" | "No." (has table AND first column is a seq pattern)
    name column     +0.25   "项目名称" | "材料名称" | "设备名称" | "品名"
    spec column     +0.15   "规格" | "型号" | "参数"
    unit & qty      +0.15   "单位" AND "数量"
    price column    +0.10   "单价" | "综合单价" | "含税价" (add if present, no penalty if absent)
    remark/brand    +0.05   "备注" | "品牌" | "厂家"
    cross-page cont +0.20   "续" | "（续）" | no header but has <table> (a previous page was recognized as a list)
    has <table>     +0.20
  gating threshold: bidlist_base_score >= 0.30 (slightly looser than the current 0.35)

category_score   = category keyword count (used only for category_guess, does not affect bidlist gating)
```

Output per page:
```json
{
  "page": 14,
  "bidlist_score": 0.85,
  "brand_score": 0.10,
  "category_guess": "valve",
  "category_confidence": 0.9
}
```

---

## 2. Category recognition

### Category keyword table

| category_id | Chinese name | Keywords (any hit scores) |
|---|---|---|
| `valve` | 阀门 (valve) | 工作压力、阀体、密封圈、阀芯、阀杆、DN+digits |
| `cable_tray` | 桥架 (cable tray) | 桥架、线槽、托盘、弯通、直通段 |
| `panel` | 配电箱/柜 (panel/cabinet) | 配电箱、配电柜、断路器、开关柜、进线柜 |
| `pipe` | 管材管件 (pipe/fittings) | 管材、管件、弯头、三通、法兰、PPR、镀锌 |
| `pump` | 水泵 (pump) | 水泵、离心泵、流量、扬程、功率(kW) |
| `hvac_diffuser` | 风口风阀 (HVAC diffuser/damper) | 风口、风阀、散流器、百叶、风量 |
| `generic` | 通用兜底 (generic fallback) | (when none of the above hit) |

Recognition strategy: for the HTML of a bidlist page, count hit keywords for each category_id, normalize, and take the highest score. Multi-category list: if the score gap between the top two hits is < 0.2, mark `multi_category=True`.

---

## 3. Selecting schema/prompt by category

### Current state

There is only one `TENDER_BIDLIST_PROMPT`, which describes the valve format (including the five material sub-columns, working pressure, etc.).

### Target design

```
CATEGORY_PROMPT_MAP = {
    "valve":         TENDER_BIDLIST_PROMPT_VALVE,
    "cable_tray":    TENDER_BIDLIST_PROMPT_CABLE_TRAY,
    "panel":         TENDER_BIDLIST_PROMPT_PANEL,
    "pipe":          TENDER_BIDLIST_PROMPT_PIPE,
    "pump":          TENDER_BIDLIST_PROMPT_PUMP,
    "hvac_diffuser": TENDER_BIDLIST_PROMPT_HVAC,
    "generic":       TENDER_BIDLIST_PROMPT_GENERIC,
}

CATEGORY_SCHEMA_MAP = {
    "valve":         TENDER_BIDLIST_SCHEMA_VALVE,      # has materials five sub-columns
    "cable_tray":    TENDER_BIDLIST_SCHEMA_CABLE_TRAY, # has material/surface_treatment
    "panel":         TENDER_BIDLIST_SCHEMA_PANEL,      # has rated_current/voltage/poles
    "pipe":          TENDER_BIDLIST_SCHEMA_PIPE,       # has material/connection_type
    "pump":          TENDER_BIDLIST_SCHEMA_PUMP,       # has flow/head/power
    "hvac_diffuser": TENDER_BIDLIST_SCHEMA_HVAC,       # has airflow/size
    "generic":       TENDER_BIDLIST_SCHEMA_GENERIC,    # spec field fallback, does not parse sub-columns
}
```

`generic` schema design: all table columns are captured as `extra_fields: {column_name: value}`, with no forced parsing, guaranteeing no data loss.

### Category detection → prompt selection flow

```
page_htmls
  → _detect_category(page_htmls, bidlist_pages)  # based on keyword count
    → category_id, category_confidence, field_mapping_hint
  → prompt = CATEGORY_PROMPT_MAP[category_id]
  → schema = CATEGORY_SCHEMA_MAP[category_id]
  → LLM(prompt, schema, llm_input)
```

If `category_confidence < 0.5` → automatically fall back to `generic`, and record `low_confidence_category: true` in quality_metrics.

---

## 4. Manual page-number confirmation and field-mapping override

### Problem scenarios

- Automatic scoring misses a continuation page (no header)
- The user's file has a non-standard header (e.g. "品目编码" instead of "序号")

### API interface extension

The existing `extract_bidlist(bidlist_pages=None, brand_page=None)` already supports manual page-number override. Add:

```python
def extract_bidlist(
    ...
    bidlist_pages: list[int] | None = None,   # existing: manually specify list pages
    brand_page: int | None = None,            # existing: manually specify brand-table page
    category_override: str | None = None,     # new: force category (skip auto detection)
    field_map_override: dict | None = None,   # new: column-name remapping {"品目编码": "seq"}
):
```

> _(corrected 2026-06-23: the actual `extract_bidlist` signature does NOT include `category_override` or `field_map_override`. Its current extra parameters are `default_category: str = "阀门"` and `xlsx_path: str | None = None`. The `bidlist_pages` / `brand_page` parameters described as "existing" are present.)_

The frontend UI (Step 2, after PDF upload) adds a collapsible "Advanced settings":
- Manual page-range input (already present)
- Category dropdown (valve / cable_tray / panel / pipe / pump / hvac_diffuser / generic)
- Field-mapping table (column name → standard field, expanded only when field_mapping_confidence < 0.7)

---

## 5. Per-page diagnostic output extension

### Current state

```json
{
  "page": 14,
  "input_mode": "html_fallback",
  "fallback_reason": "duplicate_headers",
  "expected_rows": 20,
  "extracted_rows": 19,
  "thinking_retry": false
}
```

### Goal

```json
{
  "page": 14,
  "input_mode": "html_fallback",
  "fallback_reason": "duplicate_headers",
  "expected_rows": 20,
  "extracted_rows": 19,
  "thinking_retry": false,
  "page_score": 0.85,
  "category_guess": "valve",
  "category_confidence": 0.92,
  "field_mapping_confidence": 0.78,
  "low_confidence_fields": ["materials.阀杆"]
}
```

`field_mapping_confidence` = the proportion of standard fields that were hit (known field count / expected field count).

---

## 6. Affected file list

| File | Change type | Description |
|---|---|---|
| `apps/api/services/tender_pdf.py` | refactor | Generalize `_score_page`; add `_detect_category`; add `category_override`/`field_map_override` params to `extract_bidlist`; branch `_row_to_anchor` by category |
| `apps/api/intelligence/prompts.py` | extend | Add 6 category prompts (can be split into files) |
| `apps/api/intelligence/schemas.py` | extend | Add 6 category schemas |
| `apps/api/services/canonical.py` | extend | Keep existing `extract_valve_canonical`; add category-dispatching `extract_canonical(category, ...)` |
| `apps/api/routes/analysis.py` | extend | `GET /analysis/tender-list/pdf-job/{id}` returns new diagnostic fields; consider adding `POST /analysis/tender-list/pdf-reextract` (with override params) |
| `apps/www/src/api/client.ts` | extend | `PageDiagnostic` adds `page_score / category_guess / category_confidence / field_mapping_confidence` |
| `apps/www/src/views/compare/IndexView.vue` | extend | Page diagnostics display adds page_score/category_guess; show field-mapping confirmation UI on low confidence |

---

## 7. Test coverage plan

| Scenario | fixture | Expectation |
|---|---|---|
| Valve list (current) | 金桥招标文件.pdf | category=valve, 89 rows, no missing seq |
| No DN but has spec/model | need to add cable-tray/panel fixture | category=cable_tray or panel, no missing seq |
| Multi-category list | composite tender document (valve + pipe) | multi_category=True, recognize category per page |
| Continuation page without header | a list of more than 4 pages | continuation page score ≥ 0.30 (generic base score), no dropped rows |
| Excel has extra rows but PDF is primary | existing 金桥 sample | reconcile.recommended_source=pdf, only_in_excel_reference=['90'] |
| Poor OCR quality / rotated page | a poorly scanned PDF | page_score < 0.30 → skip or manual confirmation |

---

## 8. Suggested implementation order

1. **Generalize `_score_page`** (no interface change, only weight change) → fastest payoff, does not affect valve precision
2. **`_detect_category`** (new function) → add category_guess to page_diagnostics
3. **`generic` prompt/schema** (fallback) → let other categories at least produce a result
4. **Land category prompts one by one**: cable tray first (highest priority?), then panel, and so on
5. **Manual confirmation UI** (`field_map_override`) → last, as an insurance measure

Do not start step 4 before steps 1–3 are complete; after each step, validate with a real PDF fixture, not just unit tests.

---

## 9. Things not to do

- **Do not split tender_pdf.py into multiple files** (unless line count > 600): premature splitting increases call-chain complexity
- **Do not add if/else category branches inside the valve prompt**: once category count ≥ 3, independent prompts are mandatory, otherwise tokens are wasted and the LLM gets confused
- **Do not use a generic LLM for automatic field mapping** (too expensive, too slow): when field-mapping confidence is low, show it to the user for manual confirmation; the LLM only assists with verification after confirmation
