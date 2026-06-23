# Design: Procurement-list category recognition + multi-category split + match auto-persists session

> **Status — audited 2026-06-23.** Implemented and matches current behavior. All five parts landed: `category_classify.py` exists, `/tender-list/preview` returns `category_breakdown` / content-based `detected_category`, `/tender-list/confirm` splits anchors into N sessions by `anchor.category`, and `/tender-list/match` auto-creates a confirmed session on the file-upload path. Divergences from the draft are minor: the line-number references below are stale (the endpoints moved), and `classify_category` returns a `CategoryGuess` dataclass rather than a bare `str`.
> _Originally written 2026-06-17. English translation of the Chinese original; now the authoritative version._

> Status: to be confirmed · 2026-06-17 · Related issues: project 63 review-matrix/bid-comparison-matrix 409, category dropdown incorrectly showing "给排水"

## Background and root cause

Three problems share one root: essentially the two concepts **"profession (profession)" and "category (category)" got crossed during the upload step**, and session persistence has a workflow gap.

The hierarchy (already present in `IndexView.vue:34`):

| profession | category (used by material master data / matching / session) |
|---|---|
| 电气 (electrical) | 桥架、母线槽、配电箱 (cable tray, busbar trunking, panel) |
| 给排水 (water supply & drainage) | **阀门**、不锈钢管、水箱、潜水泵 (**valve**, stainless steel pipe, water tank, submersible pump) |
| 暖通 (HVAC) | 风口风阀、风机盘管、空调泵 (diffuser/damper, fan coil unit, HVAC pump) |

Three root causes:

1. **Category auto-recognition error** (Q2): `tender-list/preview` (analysis.py:619) returned the Excel's **profession column** (给排水) as detected_category, while the dropdown options are 10 **categories**, producing the orphan value "给排水", and `Material.category=="给排水"` matched no quotes. _(corrected 2026-06-23: the preview endpoint is now at analysis.py:811, not :619. The fix is in place — `detected_category` is now the majority category from content-based `classify_category`, not the profession column.)_
2. **No multi-category support** (Q1): one TenderListSession binds a single category, and matching filters quotes only by that category; a mixed-category list crams all anchors into one category.
3. **Session workflow gap**: the `/tender-list/match` file path only linked to an existing session and never created one; when a user skipped the "confirm procurement-list version" button and jumped straight into Step 3, the session was always empty → both the review matrix and the bid-comparison matrix returned 409. _(corrected 2026-06-23: the match endpoint is now at analysis.py:930. The gap is closed — see Part 4.)_

## Decisions (confirmed with the user)

- Q2 → **infer category from item name**
- Q1 → **split into multiple sessions by category**
- Gap → **auto-persist the session on the backend at match time**

---

## Plan

### 1. Category classifier (new) `apps/api/services/category_classify.py`

```python
def classify_category(name: str, spec: str = "", pressure: str = "", material: str = "") -> str:
    """item name → one of 10 categories; returns "" when not recognized."""
```

> _(corrected 2026-06-23: the implemented `classify_category` returns a `CategoryGuess` dataclass `(category, confidence, reason)`, not a bare `str`. An empty `category` (or `confidence < CONFIDENCE_THRESHOLD = 0.6`) means unknown. The 10 categories live in `ALL_CATEGORIES` in `category_classify.py`.)_

- **Valve**: reuse `extract_valve_canonical(...)`; a non-empty `valve_type` is classified as valve (covers Y-type strainers / backflow preventers / various valves, which the existing naive substring matching mis-classifies).
- The other 9 categories: maintain a keyword table (cable tray / busbar trunking / panel / stainless steel pipe / water tank / submersible pump / diffuser / damper / fan coil unit / HVAC pump …); a substring/regex hit classifies it.
- Order: valve canonical first, then the keyword table, finally fall back to the existing `_infer_category`. _(corrected 2026-06-23: the implementation runs the keyword table FIRST — ordered most-specific-category-first so that, e.g., diffuser/damper categories are decided before the generic valve "阀" keyword — and uses `extract_valve_canonical` as the fallback. There is no call to `_infer_category` in the final classifier; normalization is done via `standardize_name`.)_
- Unit-test coverage: Y-type strainer → valve, flushing/water-intake valve → valve, pressure-reducing backflow preventer → valve, cable tray → cable tray, etc.

### 2. preview endpoint rework (analysis.py:586)

Each anchor gets a `category` field (= classify result); the response adds `category_breakdown`:

> _(corrected 2026-06-23: preview is at analysis.py:811. Implemented as described — each item carries `category`, `category_confidence`, and `category_reason`; the response includes `category_breakdown`, `detected_category`, `has_multiple_categories`, and `unknown_count`.)_

```jsonc
{
  "items": [{..., "category": "阀门"}, ...],
  "category_breakdown": {"阀门": 88, "不锈钢管": 2},   // new
  "detected_category": "阀门",   // = breakdown majority (replaces the old profession[0])
  "total": 90
}
```

The `profession` field is kept (informational) but no longer used for detected_category.

### 3. confirm endpoint rework: split into multiple sessions by category (analysis.py:1344)

The input is still all anchors; the backend groups by `anchor.category`, **creating one TenderListSession per category**:

> _(corrected 2026-06-23: confirm is at analysis.py:2026. Implemented as described. Additionally: items with an empty/unknown category are blocked with HTTP 400 unless `force=True`, in which case they are folded into `body.category` and flagged with `_category_forced=True` for audit.)_

- Single-category list (e.g. all-valve, as in this case) → 1 session, behavior identical to today.
- Multi-category → N sessions, each with `anchors_json` containing only its category's anchors, `is_current=True`/`status=confirmed`, with version numbers incremented independently per category.
- Returns `{"sessions": [{"category": "...", "id": ..., "version": ..., "anchors_total": ...}], ...}`. _(corrected 2026-06-23: the response also includes `id`/`version`/`primary_category` for the primary session — the category with the most anchors — for backward compatibility with the old frontend, plus a top-level `multi_category` flag.)_

### 4. match auto-persists session (analysis.py:659, closes the gap)

File-upload path: after parsing, if the (project, category) has no current session, **auto-create a confirmed session from the parsed anchors of that category** and then match. This guarantees "any project that has run matching from a file has a session", so re-running project 63 self-heals.

> _(corrected 2026-06-23: match is at analysis.py:930. Implemented as described. The file-upload path calls `group_anchors_by_category` and creates one confirmed session per detected category; it also validates that the requested `category` is among the categories detected in the file (HTTP 400 `category_not_in_file` otherwise) to prevent cross-category pollution.)_

For multi-category: match creates a session per category and matches per category.

### 5. Frontend Step 2 rework (IndexView.vue)

- After upload, show `category_breakdown`: "Recognized valve×88, stainless steel pipe×2".
- Single-category: keep the status quo (dropdown pre-selects the recognized category).
- Multi-category: prompt that it will be split into N parts by category; confirming creates N sessions.
- The `tenderCategory` dropdown is enabled only when recognition is empty / needs manual correction.

### 6. Downstream (Step 3 review / Step 4 bid-comparison)

`/bid-matrix`, `/anchor-review/matrix`, export, finalize all already work by (project, category) — for multi-category, add a **category switcher** (top segmented control) to review/compare per category. No need to change these interfaces themselves.

---

## Affected files

| File | Change |
|---|---|
| `apps/api/services/category_classify.py` | new classifier |
| `apps/api/services/tender_list.py` | tag anchor with category after parse (or at preview) |
| `apps/api/routes/analysis.py` | rework preview / confirm / match (three places) |
| `apps/api/services/anchor_match.py` | match supports auto-persist session + per-category |
| `apps/www/src/views/compare/IndexView.vue` | category-recognition display + multi-category split prompt + downstream category switcher |
| `apps/api/tests/` | classifier unit tests + multi-category E2E |

> _(corrected 2026-06-23: the auto-persist-session logic actually lives in the `/tender-list/match` route in `analysis.py` — it calls `group_anchors_by_category` from `tender_list.py` and `save_session` from `tender_session_service.py` — rather than inside `anchor_match.py`. `tender_list.py` provides `classify_category`-based serialization via `anchor_to_json` and `group_anchors_by_category`.)_

## Acceptance

- [ ] This sample list recognized as **valve** (not 给排水), and matching can pull quotes
- [ ] Single-category list confirm → 1 session; multi-category → N sessions, each anchors_json containing only its category
- [ ] A project that skipped manual confirmation and only ran match can still generate a session (project 63 re-run self-heals)
- [ ] Review matrix + bid-comparison matrix no longer 409, and multi-category is switchable
- [ ] Classifier unit tests pass
