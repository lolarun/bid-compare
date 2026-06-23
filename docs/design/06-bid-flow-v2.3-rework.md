# Bid-Comparison Flow v2.3 Rework Design

> **Status — audited 2026-06-23.** Implemented and since superseded. The three v2.3 fixes described here have landed (low-confidence anchors → `pending`, the `/tender-list/preview` endpoint exists, the multi-row overwrite was fixed in `bid_matrix.py`), but the architecture has moved on to **v2.5**: the matrix is now built on a `TenderListSession` + anchor-full-axis model with `BidSubmission.id` as the column identity, the `LOW_CONF` constant now lives in `apps/api/core/domain_config.py` as `MATCH_LOW_CONFIDENCE_THRESHOLD = 0.70`, and group/item confirmation is handled by `/anchor-review/item-confirm` and group endpoints rather than the single `/anchor-review/confirm` shape sketched in §2.3. Read this as a historical decision record, not current API documentation.
> _Originally written (date not recorded in source). English translation of the Chinese original; now the authoritative version._

**Background**: v2.2 had three structural problems (from a ChatGPT code audit):
1. Low-confidence anchors were written directly as `confirmed`, bypassing the manual-review gate and entering the matrix directly.
2. When the same supplier quoted multiple rows for the same procurement item, they overwrote each other (`bid_matrix.py` assigned directly).
3. The flow order did not match how procurement staff actually work (configure first → then upload, rather than upload first → then confirm).

---

## 1. New User Flow

```
Step 1  Upload procurement list
        └─ Upload .xlsx → parse preview (name / spec / quantity list)
        └─ Auto-detect category, editable
        └─ Proceed to Step 2 after confirmation

Step 2  Upload supplier quotes
        └─ Drag in multiple files, OCR recognition + supplier confirmation
        └─ After all files are stored, proceed to Step 3

Step 3  Alignment review (gate) ← core change
        ├─ System automatically runs embedding matching
        ├─ High confidence (≥0.70) → status=confirmed, shown in list
        ├─ Low confidence (<0.70) → status=pending, must be handled one by one
        │   Actions: "Confirm assignment" or "Remove"
        │   Explanatory text: "The system will assign [supplier quote X] to
        │                      [procurement item Y], similarity 63%; please
        │                      confirm whether this is reasonable"
        └─ Off-list quotes: shown with an explanation, do not enter the matrix

        [Only after ALL pending items are handled → "Generate comparison matrix" becomes clickable]

Step 4  Comparison matrix
        └─ Consumes only status=confirmed groups
```

---

## 2. Backend Changes

### 2.1 anchor_match.py — low confidence becomes pending

```python
# Old
status = "confirmed"

# New
status = "confirmed" if min_cos >= LOW_CONF else "pending"
```

The `/analysis/anchor-review` return shape is unchanged; `low_conf_groups` corresponds to the `pending` groups.

> (corrected 2026-06-23: `LOW_CONF` is now imported in `anchor_match.py` from `apps.api.core.domain_config` as `MATCH_LOW_CONFIDENCE_THRESHOLD` (= 0.70), per the "thresholds centralized" invariant. The current code branches per-item (`item_action = "align" if cos >= LOW_CONF else "pending"`) and also routes conflicting rows to pending, rather than a single group-level `min_cos` decision.)

### 2.2 New endpoint POST /analysis/tender-list/preview

Parses only the xlsx, does not run embedding, returns preview data immediately.

```json
{
  "items": [
    { "seq": 1, "name": "截止阀", "spec": "DN25 PN16", "unit": "个", "qty": 10 }
  ],
  "detected_category": "阀门",
  "total": 90
}
```

> (Confirmed 2026-06-23: `POST /analysis/tender-list/preview` exists in `apps/api/routes/analysis.py`.)

### 2.3 New endpoint POST /analysis/anchor-review/confirm

Manually confirm / remove a pending group.

```
body: { group_id: int, action: "confirm" | "reject" }
→ action=confirm: status → confirmed
→ action=reject:  DELETE group (cascade deletes items)
```

After all pending items are handled, the matrix becomes available.

> (corrected 2026-06-23: the implemented endpoints are `POST /analysis/anchor-review/item-confirm` (precise single low-confidence quote) plus group-level confirm and `DELETE /analysis/bid-alignment/groups/{group_id}`, rather than a single `/anchor-review/confirm` taking `{group_id, action}`. The semantics — confirm promotes to `confirmed`, reject deletes the group with cascade — are preserved.)

### 2.4 bid_matrix.py — fix multi-row overwrite

When the same supplier has multiple quotes for the same anchor, take the one with the **lowest unit price** into the matrix (or aggregate in the future).

```python
# Old
quote_by_supplier[item.supplier_id] = qt

# New
existing = quote_by_supplier.get(item.supplier_id)
if existing is None or (qt.unit_price or 0) < (existing.unit_price or 0):
    quote_by_supplier[item.supplier_id] = qt
```

> (corrected 2026-06-23: the current `bid_matrix.py` no longer keys columns by `supplier_id` in the main matrix path — it builds anchor rows with cells keyed by `submission_id` (or `supplier_id` depending on mode) and selects/aggregates per the `align`/`aggregated`/`pending`/`excluded`/`missing` cell-status model. The "lowest among confirmed cells" marking still exists. The naive `quote_by_supplier` overwrite the doc warned about is gone.)

### 2.5 bid_matrix.py — consume only confirmed groups

```python
# Old: all confirmed-status groups
.filter(BidAlignmentGroup.status == "confirmed")

# New (already this filter, but confirm pending does not mix in)
# pending is not in confirmed, so it is naturally isolated
```

> (Confirmed 2026-06-23: `bid_matrix.py` filters `BidAlignmentGroup.status == "confirmed"` and scopes groups to the current confirmed `TenderListSession` only, to prevent historical-data leakage.)

---

## 3. Frontend Changes (IndexView.vue)

### 3.1 Step Reorganization

| Step | Old | New |
|------|----|----|
| Step 0 | Configure task (project + category + supplier) | Configure task (project; category optional) |
| Step 1 | Upload quote files (procurement list as attachment) | **Upload procurement list** (required, shows preview) |
| Step 2 | Anchor review or AI alignment | **Upload supplier quotes** |
| Step 3 | Comparison result | **Alignment review (gate)** |
| Step 4 | — | **Comparison matrix** |

### 3.2 Step 1 — Procurement List Upload + Preview

- Upload xlsx → call `/tender-list/preview` → display table (name / spec / unit / quantity)
- Show the auto-detected category at the top; editable via a dropdown
- After the preview is confirmed, the state is saved to `tenderPreview`; only then is "Next" shown

### 3.3 Step 2 — Supplier Quotes (existing logic unchanged)

- Keep the existing multi-file drag-in + OCR + supplier-confirmation flow
- After all files are confirmed, show "Start matching" (calls `/tender-list/match`)

### 3.4 Step 3 — Alignment Review (gate rework)

- Call `/anchor-review` to fetch data
- **High confidence**: collapsed list, expandable to view detail, no action required
- **Pending**:
  - Shown one by one, cannot be skipped
  - Each shows a plain-language explanation
  - Actions: "Confirm assignment" / "Remove this item"
  - Progress bar: N/M pending items handled
  - The "Generate comparison matrix" button is disabled until all are handled
- **Off-list quotes**: informational display, no action required

### 3.5 Supplier Name Fallback

When `sup` is None, display `供应商#${supplier_id}` (supplier #…), leaving no blank.

---

## 4. Out of Scope This Round

- Canonical key / per-category attribute matching (Issue #3)
- OCR validation gate + re-read of failed pages (Issue #4)
- Source traceability (Issue #5)
- Field-correction-driven recomputation (Issue #7)
- Cleanup of legacy UI remnants (Issue #8)
