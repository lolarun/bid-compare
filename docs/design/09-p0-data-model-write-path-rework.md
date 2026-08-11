# P0 Data Model and Write-Path Rework

> **Status — audited 2026-06-23.** Partially implemented. The data model (BidSubmission / BidQuoteLine / SupplierAlias / rebuilt BidAlignmentItem / suppliers cleanup columns), the write-path hard-cut (batch-confirm, archive-prices, 7-layer supplier resolution, `valid_quote_filters()`, `detected_category`, match reading BidQuoteLine), and Phase 1 DDL are all in place; but the `archive-prices` endpoint shape changed (now `POST /api/quotes/archive-prices` with a body, not a path param), `bid_submissions.supplier_id` is now nullable, and the entire Phase 4 production-cleanup tooling (`audit_suppliers.py`, `merge_suppliers.py`, `recalculate_ref_prices.py`, the production fix-package scripts, and the ops runbook) is not yet built.
> _Originally written 2026-06-18 (v3 design draft). English translation of the Chinese original; now the authoritative version._

> **Status**: v3 design draft, code changes pending confirmation
> **Date**: 2026-06-18
> **Scope**: root-cause fix for bid-comparison data pollution · master-data isolation · supplier alias system · historical-data cleanup

---

## 1. Pollution root-cause analysis

### 1.1 `material_class` and `category` conflated

**Root cause**: `tender_pdf.py:404` returns the raw OCR text `material_class="水阀门"` separately, and `IndexView.vue:232` wrongly assigns it to `tenderCategory`, causing all downstream matching to run under the wrong category.

Actual data flow:
- `items[*].category` was already correctly set to "阀门" via `anchor_to_json(a, default_category="阀门")` ✓
- `material_class` is the raw text of the tender document, for display only — it is not the system's category key

**Fix direction**: the API adds a `detected_category` field (majority-vote over `items[*].category`); the frontend `tenderCategory` may only be assigned from this field, and `material_class` is shown only on the Step 2 conclusion card.

---

### 1.2 `batch-confirm` pollutes three master-data tables

| Code location | Polluting behavior |
|---------|---------|
| `quotes.py:360–362` | `INSERT Supplier` when no match found; the 0.75 fuzzy-match threshold is unstable for short names |
| `quotes.py:544–557` | `INSERT Material` when (category, standard_name, spec) misses |
| `quotes.py:626–648` | directly `INSERT Quote` on every confirm, which skews reference-price statistics |

---

### 1.3 No supplier alias system

The same supplier appears in different files under multiple name forms; with no historical memory, duplicate records are created.

---

### 1.4 `BidAlignmentItem.quote_id` bound to the `quotes` table

Today `quote_id NOT NULL` forces the matrix to depend on the `quotes` table, requiring the user to archive before they can compare. The correct flow is for bid-comparison to read `BidQuoteLine` end-to-end, with archiving historical prices being a separate, optional action.

---

### 1.5 Reference-price queries have no unified validity filter

`refresh_material_baselines()` and all statistics queries read every `quotes` row directly, with no way to exclude polluted bid prices, which distorts reference prices.

---

## 2. Current write path (As-Is)

```
Upload supplier PDF → ExtractionJob(done)
  │
  ▼
POST /quotes/batch-confirm
  ├─ ⚠️ no match → INSERT Supplier (master-data pollution)
  ├─ ⚠️ miss → INSERT Material (master-data pollution)
  └─ ⚠️ direct INSERT Quote (historical-price pollution)
       db.commit()

  │
  ▼
POST /analysis/tender-list/match
  → reads quotes table (already polluted) → INSERT BidAlignmentItem(quote_id=...)
  │
  ▼
BidMatrixVersion snapshot
```

---

## 3. Target write path (To-Be)

```
Upload supplier PDF → ExtractionJob(done)
  │
  ▼
Frontend: user selects from the existing supplier list
  (not found → prompt to maintain it in supplier management; ad-hoc creation forbidden)
  │
  ▼
POST /quotes/batch-confirm (reworked)
  ├─ Supplier: 7-layer alias lookup → miss → 400, creation forbidden
  ├─ Material: match by (category, standard_name, spec) → miss → material_id=NULL, creation forbidden
  └─ INSERT BidSubmission + BidQuoteLine × N
       row counts of quotes / materials / suppliers stay completely unchanged

  │
  ▼ (same release version as batch-confirm)
POST /analysis/tender-list/match (reworked)
  → reads BidQuoteLine → INSERT BidAlignmentItem(bid_quote_line_id=...)
  │
  ▼
Review / BidMatrixVersion snapshot / Excel export (reads BidQuoteLine throughout)

  │ (optional, separately triggered)
  ▼
POST /quotes/bid-submissions/{id}/archive-prices
  ├─ processes only rows where material_id IS NOT NULL (returns detail, see §4.5)
  ├─ INSERT Quote (idempotent, archived_quote_id backfilled)
  └─ BidSubmission.status set to archived / partially_archived per result
       Material / Supplier master data is still never created here
```

> _(corrected 2026-06-23: the implemented endpoint is `POST /api/quotes/archive-prices` taking a JSON body `{submission_id, project_id?}` — not the path-parameter form `POST /quotes/bid-submissions/{id}/archive-prices` shown here and in §4.5. The behavior — material_id-NULL rows skipped, idempotent archival, status tri-state — matches.)_

---

## 4. Data model design (To-Be)

### 4.1 New table `bid_submissions`

```
bid_submissions
  id                INTEGER  PK AUTOINCREMENT
  job_id            TEXT     NOT NULL → extraction_jobs(id)
  supplier_id       INTEGER  NOT NULL → suppliers(id)
                    -- must be chosen by the user from the existing list;
                    -- never empty, never auto-created by the system
  supplier_raw_name TEXT     NOT NULL DEFAULT ''    -- raw OCR name retained (for traceability)
  project_id        INTEGER  → projects(id)          -- may be NULL
  batch_id          TEXT     NOT NULL UNIQUE         -- idempotency key
  status            TEXT     NOT NULL DEFAULT 'pending'
                    -- pending / confirmed / archived / partially_archived / rejected
  bid_status        TEXT     NOT NULL DEFAULT ''
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
```

> _(corrected 2026-06-23: in the implemented model `apps/api/models/bid_submission.py`, `supplier_id` is `nullable=True`, not NOT NULL. The comment there explains the weak association: an unknown supplier can complete bid-comparison, and a real supplier is only required at archive time — `archive-prices` returns 422 if `supplier_id` is NULL. The "never auto-created" invariant still holds.)_

State machine:
```
pending → confirmed (procurement reviewer approves)
        → rejected (procurement reviewer rejects)
confirmed → archived (all rows archived successfully)
          → partially_archived (some rows skipped because material_id=NULL)
```

### 4.2 New table `bid_quote_lines`

```
bid_quote_lines
  id                  INTEGER  PK AUTOINCREMENT
  submission_id       INTEGER  NOT NULL → bid_submissions(id) ON DELETE CASCADE
  material_id         INTEGER  → materials(id)   -- may be NULL; NULL does not affect bid-comparison, skipped on archive
  raw_name            TEXT     NOT NULL DEFAULT ''
  standard_name       TEXT     NOT NULL DEFAULT ''
  category            TEXT     NOT NULL DEFAULT ''
  spec                TEXT     NOT NULL DEFAULT ''
  unit                TEXT     NOT NULL DEFAULT ''
  qty                 REAL
  unit_price          REAL
  unit_price_excl_tax REAL
  tax_rate            REAL
  total_price         REAL
  brand               TEXT     NOT NULL DEFAULT ''
  brand_tier          TEXT     NOT NULL DEFAULT ''
  remark              TEXT     NOT NULL DEFAULT ''
  quote_date          TEXT     NOT NULL DEFAULT ''
  canonical           JSON     -- structured key (valve_type/DN/PN), for intra-project soft alignment
  extraction_meta     JSON     -- full OCR evidence
  deviation_pct       REAL
  alert_level         TEXT     NOT NULL DEFAULT ''
  archived_quote_id   INTEGER  → quotes(id)       -- backfilled after archive, NULL = not archived
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP

INDEX ix_bql_submission (submission_id)
INDEX ix_bql_material   (material_id)
```

> _(corrected 2026-06-23: the implemented `BidQuoteLine` additionally carries `updated_at` (row-level audit timestamp, P1-3) and `row_type` (`quote_line|section_header|remark|invalid|subtotal|grand_total`, P1-3 — the confirmed row-type snapshot; existing rows backfilled to `'quote_line'`). These columns were added by later P1 work and are not in this P0 draft.)_

**Alignment capability with `material_id=NULL`**: intra-project alignment and the matrix use `canonical` (valve_type/DN/PN) + `standard_name` + `spec` for soft alignment, with no need for `material_id`. Only explicitly archiving historical prices requires `material_id IS NOT NULL`.

### 4.3 New table `supplier_aliases`

```
supplier_aliases
  id               INTEGER  PK AUTOINCREMENT
  supplier_id      INTEGER  NOT NULL → suppliers(id) ON DELETE CASCADE
  alias            TEXT     NOT NULL DEFAULT ''
                   -- raw alias text (kept for display/traceability, no unique constraint)
  normalized_alias TEXT     NOT NULL
                   -- normalized text (see noise-stripping rules)
  alias_type       TEXT     NOT NULL DEFAULT 'historical'
                   -- legal_name / short_name / filename / historical
  active           INTEGER  NOT NULL DEFAULT 1   -- 1=enabled, 0=disabled
  confidence       REAL     NOT NULL DEFAULT 1.0
  created_by       TEXT     NOT NULL DEFAULT ''  -- 'system_init' / 'user:xxx' / 'ocr_auto'
  source_reference TEXT     NOT NULL DEFAULT ''  -- e.g. "filename: 凯硕新正投标文件.pdf"
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP

UNIQUE (supplier_id, normalized_alias, alias_type)
  -- one supplier may keep multiple evidence records from different sources
  -- "凯硕新正"(short_name) and "凯硕新正投标文件.pdf"(filename, → 凯硕新正 after stripping)
  -- may coexist on the same supplier

INDEX ix_sa_normalized (normalized_alias)   -- ordinary index, used for cross-supplier queries
INDEX ix_sa_supplier   (supplier_id)
```

**Unique-constraint design note**:

`UNIQUE(supplier_id, normalized_alias, alias_type)` rather than `UNIQUE(normalized_alias)`:
- allows one supplier to keep multiple evidence records of different `alias_type` (`short_name="凯硕新正"` and `filename="凯硕新正"`)
- allows the same `normalized_alias` to temporarily point at multiple suppliers during triage (`ambiguous` state, requires manual resolution)

**When `normalized_alias` hits multiple distinct `supplier_id`s**:
- return `{"error": "supplier_ambiguous", "candidates": [{supplier_id, name, alias_type}]}`
- automatic selection is forbidden; the frontend displays the candidate list for the user to choose manually
- after the user chooses, the alias is written to the chosen supplier with `alias_type='historical'`; the same `normalized_alias` alias on other suppliers is marked `active=0`

**Filename noise-stripping rules** (run before generating `normalized_alias`):

Process in the following order; lowercase the result and trim leading/trailing whitespace:
1. Strip file extensions: `.pdf` `.xlsx` `.xls` `.docx` `.doc`
2. Strip noise words: `投标文件` `报价单` `报价文件` `采购清单` `招标文件` `第一轮` `第二轮` `第一次` `第二次` `终稿` `定稿` `修改版` `最终版` `v1` `v2` `v3`
3. Strip date patterns: `\d{4}[-_年]\d{1,2}[-_月]\d{1,2}[日]?` and `\d{6,8}`
4. Strip whitespace-only Chinese/English bracket pairs (empty strings)

**7-layer lookup priority** (`_resolve_supplier()` function):

| Layer | Method | Notes |
|----|------|------|
| 1 | `supplier_id` passed in directly | user manually selected in the frontend (highest priority) |
| 2 | `Supplier.name` exact match | master-data original name |
| 3 | `alias_type='legal_name'` `normalized_alias` exact match | business-license full name |
| 4 | `alias_type='short_name'` `normalized_alias` exact match | everyday short name |
| 5 | `alias_type='filename'` `normalized_alias` exact match (after noise stripping) | filename source |
| 6 | `alias_type='historical'` `normalized_alias` exact match | historical OCR confirmation |
| 7 | `difflib` fuzzy match ≥ 0.85 (all active aliases + `Supplier.name`) | returns candidate list, no auto-match |

Layers 1–6 unique hit: return the `supplier_id` directly.
Layers 1–6 no hit, layer 7 has candidates: return `{"error": "supplier_ambiguous", "candidates": [...]}` — user chooses manually.
Layer 7 also no hit: return `{"error": "supplier_not_found"}` — the frontend prompts the user to create it in supplier management; creation is forbidden within the bid-comparison flow.

> _(corrected 2026-06-23: the implemented service is `apps/api/services/supplier/supplier_resolve.py`, function `resolve_supplier()` (no leading underscore), returning a `ResolveResult` dataclass (`supplier` / `candidates` / `matched_layer` / `normalized`) rather than the raw `{"error": ...}` dicts described here. The layer ordering also differs: layer 1 is `Supplier.name` exact (case-insensitive), layer 2 is `Supplier.short_name` exact, and layers 3–6 are the four `SupplierAlias` types — the explicit "supplier_id passed in directly" is handled by the caller, not inside the function. Routes map the dataclass to HTTP responses.)_

**Alias seed data** (written via a migration script, not hard-coded in business if/else):

Write only the known alias forms of suppliers **already present in the existing `suppliers` table**.

**Correct alias sources** (`Quote.extraction_meta_json.raw_material` is the raw material text, not a supplier name, and is forbidden for generating supplier aliases):

| Source | Field path | alias_type |
|------|---------|------------|
| The master-data name itself | `Supplier.name` | `legal_name` or `short_name` (the script decides by length/format) |
| OCR-recognized supplier name | `ExtractionJob.result.supplier_name` / `ExtractionJob.context.supplier_name` | `historical` |
| Quote filename (after stripping) | `ExtractionJob.filename` → noise strip → `normalized_alias` | `filename` |
| Manual confirmation record | manually entered | any type, `created_by='user:xxx'` |

Names not proven by historical data (i.e. with no corresponding record in `ExtractionJob` or `Supplier`) must not be written as seeds.

Migration-script logic (see §5.2 Phase 1):
1. Query all rows of the `suppliers` table; for each row generate an `alias_type='legal_name'` seed from `Supplier.name`
2. Query all `ExtractionJob`s, associate to a known `supplier_id` via `context.supplier_id` / `result.supplier_id`, generate `historical` seeds from `result.supplier_name` and `context.supplier_name`, and generate `filename` seeds from `filename` after noise stripping
3. On conflict (the same `normalized_alias` matches multiple suppliers after normalization), write a conflict report file; do not auto-resolve

---

### 4.4 Rebuild `bid_alignment_items` (full SQLite table rebuild)

**Current state**: `quote_id INTEGER NOT NULL`, which cannot be made nullable via `ADD COLUMN` alone.

**Rebuild target**:

```
bid_alignment_items (after rebuild)
  id                  INTEGER  PK AUTOINCREMENT
  group_id            INTEGER  NOT NULL → bid_alignment_groups(id) ON DELETE CASCADE
  quote_id            INTEGER  → quotes(id)          -- may be NULL (historical path)
  bid_quote_line_id   INTEGER  → bid_quote_lines(id) -- may be NULL (new path)
  supplier_id         INTEGER  → suppliers(id)
  action              TEXT     DEFAULT 'align'
  spec_note           TEXT     DEFAULT ''
  agg_total           REAL
  agg_qty             REAL
  name_note           TEXT     DEFAULT ''
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP

  CHECK (
    (quote_id IS NOT NULL AND bid_quote_line_id IS NULL)
    OR
    (quote_id IS NULL AND bid_quote_line_id IS NOT NULL)
  )  -- exactly one of the two must be non-null

UNIQUE INDEX ix_align_item_group_quote   (group_id, quote_id)         WHERE quote_id IS NOT NULL
UNIQUE INDEX ix_align_item_group_bql     (group_id, bid_quote_line_id) WHERE bid_quote_line_id IS NOT NULL
```

> _(corrected 2026-06-23: the implemented `BidAlignmentItem` additionally carries a `submission_id` column (§11.2, soft FK to `bid_submissions`). The CHECK constraint and both partial unique indexes are created by `_ensure_sqlite_schema()` as described below.)_

**Full rebuild steps (SQLite protocol)**:

```
Step 0: backup
  cp data/mempas.db data/mempas-before-p0-YYYY-MM-DD.bak
  -- run before any DDL; do not skip

Step 1: record pre-migration row counts (validation baseline)
  SELECT COUNT(*) FROM bid_alignment_items;                → call it N_before
  SELECT COUNT(*) FROM bid_alignment_groups;               → call it G_before
  SELECT COUNT(*) FROM quotes WHERE id IN
    (SELECT quote_id FROM bid_alignment_items);            → call it FK_before

Step 2: PRAGMA foreign_keys = OFF
  -- foreign-key checks must be temporarily disabled during a SQLite table rebuild

Step 3: CREATE TABLE bid_alignment_items_new (...)
  -- with all new column definitions and the CHECK constraint (see above)
  -- without the UNIQUE INDEXes (created in the next step)

Step 4: INSERT INTO bid_alignment_items_new
  SELECT id, group_id, quote_id, NULL as bid_quote_line_id,
         supplier_id, action, spec_note, agg_total, agg_qty, name_note, created_at
  FROM bid_alignment_items;
  -- full migration; bid_quote_line_id initialized to NULL

Step 5: validate row count
  SELECT COUNT(*) FROM bid_alignment_items_new;            → must equal N_before
  if not equal: ROLLBACK (see rollback plan)

Step 6: DROP TABLE bid_alignment_items

Step 7: ALTER TABLE bid_alignment_items_new RENAME TO bid_alignment_items

Step 8: CREATE UNIQUE INDEX ix_align_item_group_quote
  ON bid_alignment_items(group_id, quote_id)
  WHERE quote_id IS NOT NULL;

Step 9: CREATE UNIQUE INDEX ix_align_item_group_bql
  ON bid_alignment_items(group_id, bid_quote_line_id)
  WHERE bid_quote_line_id IS NOT NULL;

Step 10: PRAGMA foreign_keys = ON

Step 11: foreign-key integrity check
  -- check that all legacy quote_id values are still valid
  SELECT COUNT(*) FROM bid_alignment_items bai
  LEFT JOIN quotes q ON bai.quote_id = q.id
  WHERE bai.quote_id IS NOT NULL AND q.id IS NULL;         → must be 0

  SELECT COUNT(*) FROM bid_alignment_items bai
  LEFT JOIN bid_alignment_groups g ON bai.group_id = g.id
  WHERE g.id IS NULL;                                      → must be 0

Step 12: final row-count conservation check
  SELECT COUNT(*) FROM bid_alignment_items;                → must equal N_before
```

**Rollback plan (step 5 fails or step 11 ≠ 0)**:

```
PRAGMA foreign_keys = OFF;
DROP TABLE IF EXISTS bid_alignment_items_new;
DROP TABLE IF EXISTS bid_alignment_items;
ALTER TABLE bid_alignment_items_bak RENAME TO bid_alignment_items;  -- if already renamed
PRAGMA foreign_keys = ON;
-- if already past the DROP step: restore from the backup file
cp data/mempas-before-p0-YYYY-MM-DD.bak data/mempas.db
```

If the rebuild fails, deploying Phase 2/3 code is **forbidden**.

---

### 4.5 `archive-prices` endpoint response format

```
POST /quotes/bid-submissions/{id}/archive-prices

Response:
{
  "submission_id": 42,
  "eligible_count": 30,      -- total BidQuoteLine rows in the submission
  "archived_count": 27,      -- rows successfully written to Quote this run (incl. idempotent historical hits)
  "skipped_count": 3,        -- skipped rows
  "skipped_lines": [
    {"line_id": 101, "raw_name": "DN50 蝶阀",  "reason": "material_id is null"},
    {"line_id": 105, "raw_name": "安装费",     "reason": "material_id is null"},
    {"line_id": 112, "raw_name": "蝶阀DN65", "reason": "duplicate archived_quote_id already set"}
  ],
  "status": "archived" | "partially_archived" | "no_eligible"
}

Status rules:
  archived           → skipped_count == 0
  partially_archived → skipped_count > 0 AND archived_count > 0
  no_eligible        → archived_count == 0

BidSubmission.status is updated in sync to the status value in the response
(so "archived" never masks a skip)
```

> _(corrected 2026-06-23: the implemented endpoint is `POST /api/quotes/archive-prices` with body `{submission_id, project_id?}`. It returns 422 if `submission_id`'s submission has a NULL `supplier_id`, and 409 if already fully archived. `skipped_lines` reasons read "material_id is NULL — cannot archive without material link" (already-archived rows are excluded from `eligible_count`, not surfaced as a skip), and the status semantics are: `no_eligible` = no eligible and no previously archived rows; `archived` = no NULL-material rows and all eligible archived without error; `partially_archived` = anything else.)_

---

### 4.6 New cleanup-marker columns on the supplier table

`suppliers` gains two columns (`ADD COLUMN`, additive):

```
ALTER TABLE suppliers ADD COLUMN merge_status TEXT NOT NULL DEFAULT 'active';
  -- active   → in normal use
  -- merged   → merged into another supplier (pointed to by merged_into_supplier_id)
  -- inactive → invalid / test-created, deactivated

ALTER TABLE suppliers ADD COLUMN merged_into_supplier_id INTEGER REFERENCES suppliers(id);
  -- non-null only when merge_status='merged'
  -- forbidden to encode the ID in a string (must not use the "merged_into:123" format)
```

---

### 4.7 Unified "valid historical quote" query condition

**Problem**: `refresh_material_baselines()`, supplier recommendation, historical-price statistics, and deviation calculation each query all of `quotes` directly, cannot jointly exclude polluted records, and are maintained in scattered places.

**Solution**: define a shared query builder in `apps/api/services/history/quote_filters.py` (new); every module touching reference prices calls this function and must not query the raw `quotes` table directly.

Query condition (expressed in SQLAlchemy):

```
valid historical quote = quotes satisfying ALL of:
  1. bid_status NOT IN ('polluted', 'excluded_from_ref', 'test')
  2. the suppliers.merge_status of supplier_id = 'active'
     (i.e. not associated with a merged or deactivated supplier)
  3. unit_price IS NOT NULL AND unit_price > 0
```

All of the following must call this shared function (no bypassing):
- `refresh_material_baselines()` — reference-price recompute
- `build_anchor_review_matrix()` — historical-price reference shown in the matrix
- supplier recommendation (`BidInvitation` scoring)
- the `ref_price_*` fields returned by `GET /materials/{id}`

> _(corrected 2026-06-23: implemented in `apps/api/services/history/quote_filters.py` as `valid_quote_filters()` (plus a `valid_quote_query(db)` convenience). The implemented filter excludes `bid_status IN ('polluted','excluded_from_ref')` and requires `Supplier.merge_status == 'active'`; it does NOT include the `'test'` bid_status exclusion nor the `unit_price > 0` clause from this draft — those conditions are applied (or not) by individual callers rather than baked into the shared filter.)_

---

### 4.8 Fix the `detected_category` field

The backend `tender_pdf.py` return structure gains `detected_category`:
- a `Counter` over `items[*].category`
- filled when the majority value > 60% (e.g. "阀门"), otherwise returns `""` (frontend prompts for manual selection)
- `material_class` is returned unchanged, for display only

Frontend `IndexView.vue` `onTenderJobDone` fix:
```typescript
// old (wrong): tenderCategory.value = r.material_class
// new (correct):
if (!tenderCategory.value && r.detected_category) {
  tenderCategory.value = r.detected_category   // "阀门"
}
// r.material_class is used only for the "tender raw category" display on the Step 2 conclusion card
```

> _(corrected 2026-06-23: `detected_category` is implemented and the frontend `IndexView.vue` assigns `tenderCategory` from `detected_category` (around lines 128/236), confirming the fix. The majority-vote is implemented in `apps/api/services/ingestion/category_classify.py` and surfaced both in `tender_pdf.py` (`most_common(1)`) and `analysis.py`; whether the strict >60% threshold and the `""` fallback are enforced may differ from this draft — the category is derived from the item names/specs, not the discipline column.)_

---

## 5. Migration and cutover plan

### 5.1 Overall principles

**No dual-write**: Phase 2/3 is a one-time hard cut. After deployment, `batch-confirm` immediately writes only the staging layer and no longer writes `quotes` / `materials` / `suppliers`.

**Cleanup order (user-confirmed)**:
1. Close the old write path (deploy Phase 2/3)
2. Run a dry-run historical-data audit
3. Manually confirm the supplier merge mapping
4. Execute cleanup and reference-price recompute

---

### 5.2 Phase 1 — DDL only (independently deployable)

All of the following are additive and do not break existing functionality:

```
1. CREATE TABLE bid_submissions
2. CREATE TABLE bid_quote_lines  (with both indexes)
3. CREATE TABLE supplier_aliases (with UNIQUE(supplier_id, normalized_alias, alias_type) and ordinary indexes)
4. ALTER TABLE suppliers ADD COLUMN merge_status TEXT NOT NULL DEFAULT 'active'
5. ALTER TABLE suppliers ADD COLUMN merged_into_supplier_id INTEGER REFERENCES suppliers(id)
6. full rebuild of bid_alignment_items (per §4.4 steps 0–12)
   -- this is a SQLite rebuild, must run offline, must back up first
```

> _(corrected 2026-06-23: Phase 1 DDL is implemented, but the project has since adopted Alembic. The legacy `_ensure_sqlite_schema()` path (which performs the additive `ADD COLUMN`s, the `bid_alignment_items` rebuild, and creates the CHECK constraint + partial unique indexes) is now FROZEN, and `_run_alembic_upgrade()` runs after it (stamps `0001_baseline` if unversioned, then upgrades to head). New persistent fields go through versioned migrations under `apps/api/migrations/versions/` (e.g. `0002_bql_updated_at`, `0003_audit_fields`, `0004_soft_fk`). See `docs/design/13`.)_

Verify immediately after Phase 1 deployment:
- `bid_alignment_items` row count = pre-migration N_before
- all `quote_id` foreign keys valid (step 11 query result is 0)
- no anomalies in existing functionality (matrix, quote history, supplier list all normal)

After Phase 1 completes, immediately run the supplier audit script (`audit_suppliers.py --dry-run`) to establish a pollution baseline; do not wait for Phase 2/3.

---

### 5.3 Phase 2/3 — application-layer hard cut (must ship in the same version)

**The following must ship in the same application version and must not be split**:

**Backend**:
- `batch-confirm`: remove `INSERT Supplier/Material/Quote`, rewrite to `BidSubmission + BidQuoteLine`
- `_resolve_supplier()`: 7-layer alias lookup (reads `supplier_aliases`)
- `POST /quotes/bid-submissions/{id}/archive-prices`: new endpoint
- `POST /analysis/tender-list/match`: change to read `BidQuoteLine`, write `BidAlignmentItem.bid_quote_line_id`
- matrix / review / export: read `BidQuoteLine` first (via `bid_quote_line_id`), fall back to `Quote` (via `quote_id`, for historical-data compatibility)
- `quote_filters.py`: new, defines the valid-historical-quote query, replaces every place that queries raw `quotes` directly
- `tender_pdf.py`: add the `detected_category` field

**Frontend**:
- `IndexView.vue`: fix the `tenderCategory` assignment in `onTenderJobDone`
- Step 1/3 supplier selection: choose from a dropdown of existing suppliers; show prompt text when not found; ad-hoc creation forbidden
- quote-storage dialog: clearly label "staging for review" (does not affect historical prices) vs "archive historical prices" (a separate operation)

**A full E2E must be run before go-live** (see §7 acceptance criteria); no functional gap is allowed.

> _(corrected 2026-06-23: this is implemented, with the endpoint-shape difference noted in §3/§4.5. `confirm_batch` lives in `apps/api/services/submission/quote_confirmation_service.py` (the route delegates to it), the resolver is `resolve_supplier()` in `apps/api/services/supplier/supplier_resolve.py`, and `analysis.py` match reads `bid_quote_line_id` with `quote_id` fallback as described.)_

---

### 5.4 Supplier-management page behavior

**Default query condition**: `Supplier.merge_status = 'active'`

Suppliers with `merge_status='merged'` and `merge_status='inactive'` are hidden by default; the user can toggle them via a "view merged / deactivated" filter.

**List display fields (per row)**:

| Field | Data source |
|------|---------|
| Supplier name (`Supplier.name`) | master data |
| Short name | the first `alias_type='short_name'` in `supplier_aliases` |
| All aliases | the list of `active=1` `alias` values in `supplier_aliases` (collapsed display) |
| Historical quote count | COUNT under the `quote_filters.py` valid-historical-quote condition |
| Project count | COUNT DISTINCT project_id of valid Quotes |
| Most recent quote date | MAX quote_date of valid Quotes |
| Merge-status label | `merge_status` (active=no label, merged="merged → {canonical_name}", inactive="deactivated") |

**Alias-management entry (per row, when expanded)**:
- view all `supplier_aliases` records for this supplier (with type, source, confidence)
- manually add an alias (alias_type / alias text)
- mark an alias `active=0` (soft delete)
- merging suppliers is not allowed on this page (merging may only be done via `merge_suppliers.py`, preserving the audit trail)

---

### 5.5 Rollback strategy

When a severe problem is found after Phase 2/3 go-live:

**Disallowed action**: restoring the old `batch-confirm` writes to `quotes / materials / suppliers` (would re-pollute).

**Correct handling**:
1. Temporarily close the "staging for review" entry (frontend feature flag or backend 503)
2. Keep the already-written `BidSubmission / BidQuoteLine` data (do not delete)
3. Fix the bug, release the fix
4. Reopen the entry, continue processing the backlog of staged records

If Phase 1 (DDL) needs rollback: restore the whole database file directly from the backup `.bak` (SQLite file-level rollback). A Phase 1 rollback loses all data written after Phase 1; confirm the business impact before executing.

(Note: the old §5.4 has been renumbered to §5.5; the supplier-management page spec is the newly added §5.4.)

---

## 6. Historical-data cleanup

### 6.1 Cleanup principles

- **No direct deletion**: mark `merge_status='merged'`/`'inactive'`, `bid_status='excluded_from_ref'`; keep traceable.
- **Dry-run first**: default `--dry-run`, output a report, execute after human confirmation.
- **Idempotent**: scripts can be re-run.
- **Quote is evidence, not a source of trust**: a historical Quote record may itself be polluted; judge using multiple signals.
- **Must not auto-merge on string similarity alone**: human confirmation is the sufficient condition.

---

### 6.2 Supplier audit report (`scripts/audit_suppliers.py`)

Output directory `supplier_audit_YYYY-MM-DD/`, containing these sub-reports:

**① `supplier_quote_stats.csv`** — historical-quote facts per supplier

Fields:

| Field | Notes |
|------|------|
| `supplier_id` | supplier ID |
| `supplier_name` | supplier name |
| `quote_count` | total historical Quote rows (all bid_status) |
| `valid_quote_count` | valid historical Quote rows (bid_status not in the polluted/excluded_from_ref list) |
| `project_count` | number of distinct projects involved |
| `earliest_quote_date` | earliest quote date |
| `latest_quote_date` | most recent quote date |
| `total_amount` | sum of all Quote amounts |
| `valid_amount` | sum of valid Quote amounts |
| `batch_id_count` | number of distinct batch_ids |
| `job_ids` | list of related ExtractionJob IDs (for source tracing) |
| `job_filenames` | list of corresponding filenames |
| `bid_status_breakdown` | `{bid_status: count}` distribution |
| `has_test_filename` | filename contains keywords like "测试"/"test"/"demo" |
| `created_at` | supplier record creation time |
| `classification` | the script's preliminary classification (see rules below) |
| `human_verdict` | manual-confirmation column (initially empty; human fills `keep/merge_into:{id}/inactive`) |

**Classification rules** (the script tags; human gives final confirmation; not directly executable):

| Classification | Logic |
|------|---------|
| `candidate_valid` | `valid_quote_count >= 2` AND `batch_id_count >= 2` AND `project_count >= 1` AND `NOT has_test_filename` |
| `single_batch_only` | `batch_id_count == 1` (only one bulk import, possibly test or pollution) |
| `no_valid_quotes` | `valid_quote_count == 0` |
| `test_keyword` | `has_test_filename = True` or supplier name contains test keywords |
| `needs_review` | none of the above conditions met |

Classification `candidate_valid` is **not** the same as confirmed-real: it only means the data features fit; human confirmation is still required.

**② `duplicate_clusters.csv`** — suspected duplicate supplier-name clusters

Algorithm: pairwise similarity (difflib ≥ 0.70) over all `Supplier.name` + all active `SupplierAlias.alias`, cluster, and output:

| Field | Notes |
|------|------|
| `cluster_id` | cluster number |
| `supplier_ids` | all supplier_ids in the cluster (comma-separated) |
| `supplier_names` | corresponding names |
| `suggested_canonical_id` | suggested keeper (highest `valid_quote_count`) — for reference only |
| `suggested_aliases` | alias forms suggested for writing into `supplier_aliases` |
| `quotes_to_migrate` | if merged, number of Quote rows to migrate (across all duplicates) |
| `bid_alignment_items_to_migrate` | number of `BidAlignmentItem.supplier_id` rows to migrate |
| `bid_invitations_to_migrate` | number of `BidInvitation.supplier_id` rows to migrate |
| `bid_submissions_to_migrate` | number of `BidSubmission.supplier_id` rows to migrate |
| `json_snapshot_affected` | number of JSON snapshots containing this supplier_id (`BidMatrixVersion` etc., record only, no rewrite) |
| `human_verdict` | manual-confirmation column (`canonical:{id}` / `all_inactive` / `split:...`) |

**③ `canonical_suppliers.csv`** — the post-cleanup set of valid suppliers (final output file)

This file represents the final set of valid suppliers that supplier management should display after cleanup, and also serves as the data basis for the supplier-management page.

| Field | Notes |
|------|------|
| `canonical_supplier_id` | the retained canonical supplier_id |
| `legal_name` | business-license full name (from `alias_type='legal_name'`, else `Supplier.name`) |
| `short_name` | everyday short name (from `alias_type='short_name'`) |
| `aliases` | the list of all active alias `alias` texts for this supplier (JSON array) |
| `historical_quote_count` | valid historical Quote rows (per the §4.7 valid-historical-quote condition) |
| `project_count` | number of distinct projects involved |
| `earliest_quote_date` | earliest quote date |
| `latest_quote_date` | most recent quote date |
| `merge_status` | current status (`active` / `merged` / `inactive`) |
| `merged_supplier_ids` | list of supplier_ids merged into this one (JSON array, empty if none) |
| `human_verdict` | human-confirmation result (`keep` / `merge_into:{id}` / `inactive`) |

**④ `conservation_check.csv`** — pre/post-merge conservation check (dry-run simulation)

For each cluster to be merged, simulate the merge and output:

| Field | Notes |
|------|------|
| `cluster_id` | cluster number |
| `before_quote_count` | total Quote rows of each supplier_id before merge |
| `after_quote_count` | after migration (all repointed to canonical_id), must equal before |
| `before_total_amount` | total amount before merge |
| `after_total_amount` | total amount after merge, must be conserved |
| `before_project_ids` | set of project_ids before merge |
| `after_project_ids` | set of project_ids after merge (should = the union of before) |
| `align_items_before` | `BidAlignmentItem.supplier_id` rows before merge |
| `align_items_after` | rows after merge, must equal before |
| `invitations_before` | `BidInvitation.supplier_id` rows before merge |
| `invitations_after` | rows after merge, must equal before |
| `conservation_ok` | True when all conservation checks pass |

---

### 6.3 Execute merge (`scripts/merge_suppliers.py`)

```
Inputs:
  --merge-plan FILE    duplicate_clusters.csv after human_verdict is filled in
  --db PATH            database path
  --dry-run            default; only output SQL and affected row counts
  --execute            actually write to the DB (must be passed explicitly)

Merge operation (per cluster; only clusters with conservation_ok=True execute):

  Validation phase:
    re-run conservation_check; skip with a warning if it fails

  Foreign-key migration phase (cover everything, leave nothing out):
    UPDATE quotes            SET supplier_id = canonical_id WHERE supplier_id IN (dup_ids)
    UPDATE bid_submissions   SET supplier_id = canonical_id WHERE supplier_id IN (dup_ids)
    UPDATE bid_alignment_items SET supplier_id = canonical_id WHERE supplier_id IN (dup_ids)
    UPDATE bid_invitations   SET supplier_id = canonical_id WHERE supplier_id IN (dup_ids)
    -- other tables with a supplier_id FK (enumerate exhaustively via PRAGMA foreign_key_list before migrating)

  Alias migration:
    INSERT INTO supplier_aliases (supplier_id, alias, normalized_alias, alias_type, ...)
    SELECT canonical_id, alias, normalized_alias, alias_type, ...
    FROM supplier_aliases WHERE supplier_id IN (dup_ids) AND active = 1
    ON CONFLICT(supplier_id, normalized_alias, alias_type) DO NOTHING
    -- migrate all duplicate aliases into canonical

  Mark duplicates:
    UPDATE suppliers
    SET merge_status='merged', merged_into_supplier_id=canonical_id
    WHERE id IN (dup_ids)

  JSON snapshot handling:
    do NOT rewrite JSON fields like BidMatrixVersion.matrix_json (historical snapshots are read-only)
    output json_snapshot_impact.csv: record which snapshots contain the merged supplier_id, for human awareness

  Post-execution check (re-run conservation_check after merge; must still be True)

Outputs:
  merge_execution_log_YYYY-MM-DD.csv (one row per operation, with affected row counts)
  json_snapshot_impact.csv
```

---

### 6.4 Reference-price recompute

After cleanup completes, call `refresh_material_baselines()` to recompute reference prices. That function must use the valid-historical-quote query condition defined in §4.7 (via `quote_filters.py`), automatically excluding Quotes with `bid_status='polluted'/'excluded_from_ref'` and Quotes associated with `merge_status != 'active'` suppliers.

Separately mark known polluted records:
```sql
UPDATE quotes SET bid_status='excluded_from_ref'
WHERE supplier_id IN (
    SELECT id FROM suppliers WHERE merge_status IN ('merged', 'inactive')
);
```

After recomputing all `material_id`s, output `ref_price_changes.csv` with these fields:

| Field | Notes |
|------|------|
| `material_id` | material ID |
| `standard_name` | material name |
| `category` | category |
| `before_ref_price_median` | median reference price before recompute |
| `after_ref_price_median` | median reference price after recompute |
| `change_pct` | change magnitude (positive or negative) |
| `before_sample_count` | valid-Quote sample count before recompute |
| `after_sample_count` | valid-Quote sample count after recompute (after excluding pollution) |
| `excluded_quotes` | list of excluded Quote IDs and exclusion reasons |
| `needs_review` | True when the change magnitude exceeds ±30% (manual review required) |

The reference price may legitimately rise (after excluding abnormally low prices) or fall (after excluding inflated bid prices). When `needs_review=True`, the cause of the change must be human-confirmed; do not use the direction of the change as the basis for judgment.

---

## 7. Offline cleanup procedure for the production database

> **Precondition**: until a production-database copy is obtained and the §7.1 validation completes, do not guess the actual merge list, and do not perform any production data modification.

---

### 7.1 Retrieving the production database

**Step one: confirm environment state**

Log in to the production server and record the following (write to `snapshot_manifest.json`):
```
server_host       101.37.166.68
db_path           /opt/mempas/data/mempas.db
db_size_bytes     (ls -l output)
current_write_status  (check for active writes: lsof /opt/mempas/data/mempas.db)
application_version   (image tag from docker compose ps)
snapshot_time     (ISO 8601, from the server clock)
```

**Step two: create a consistent snapshot (must use the SQLite backup API)**

Directly `cp mempas.db` while the application is running is forbidden (may read an inconsistent state).
Use either of:

Method A — `sqlite3` command line (recommended):
```
sqlite3 /opt/mempas/data/mempas.db \
  ".backup /opt/mempas/data/mempas-snapshot-<timestamp>.db"
```

Method B — Python `sqlite3.Connection.backup()` (called in a script):
```python
import sqlite3
src = sqlite3.connect("/opt/mempas/data/mempas.db")
dst = sqlite3.connect("/opt/mempas/data/mempas-snapshot-<timestamp>.db")
src.backup(dst)
```

The backup API guarantees consistency in SQLite WAL mode, with no need to stop the application.

**Step three: record baseline validation values (write to `snapshot_manifest.json`)**

```
snapshot_file      mempas-snapshot-<timestamp>.db
snapshot_sha256    (sha256sum output)
row_counts:
  suppliers        SELECT COUNT(*) FROM suppliers
  quotes           SELECT COUNT(*) FROM quotes
  materials        SELECT COUNT(*) FROM materials
  bid_alignment_items  SELECT COUNT(*) FROM bid_alignment_items
  supplier_aliases     SELECT COUNT(*) FROM supplier_aliases (if created)
schema_version     (SELECT name FROM sqlite_master WHERE type='table' ORDER BY name)
```

**Step four: scp the snapshot to local, keep the original snapshot read-only**

```
scp root@101.37.166.68:/opt/mempas/data/mempas-snapshot-<timestamp>.db ./data/
chmod 444 data/mempas-snapshot-<timestamp>.db   # set read-only to prevent accidental modification
```

---

### 7.2 Local file layering

The local working directory `data/production-cleanup-<timestamp>/` contains the following files, named in strict correspondence:

| File | Purpose | Operation rights |
|------|------|---------|
| `production-original-<timestamp>.db` | original read-only copy (from scp) | read-only, modification forbidden |
| `production-working-<timestamp>.db` | working copy for cleanup (copied from original) | read/write, cleanup runs on this file |
| `production-cleaned-<timestamp>.db` | validated result copy (copied from working) | read-only, used for smoke testing |
| `snapshot_manifest.json` | original baseline info (see §7.1 step three) | read-only |
| `canonical_suppliers.csv` | human-confirmed supplier merge plan (see §6.2 ③) | human-filled |
| `merge_plan.csv` | `merge_suppliers.py` input (exported from canonical_suppliers.csv) | human-filled |
| `audit_report/` | `audit_suppliers.py` output directory | read-only, generated by script |
| `fix_package/` | production write-back package (see §7.4) | read-only, generated by build script |

**Reports and human-confirmed plans must be bound to the original database SHA256**: every script output file records `source_db_sha256` in its header, ensuring the cleanup plan corresponds strictly to the data version, so that a plan made at time A is never applied to the database at time B.

How to create the working copy:
```
cp data/production-cleanup-<ts>/production-original-<ts>.db \
   data/production-cleanup-<ts>/production-working-<ts>.db
```

---

### 7.3 Local offline cleanup flow

Run the following steps on `production-working-<timestamp>.db`, validating with `--dry-run` first throughout:

**Step one: run supplier audit**
```
python scripts/audit_suppliers.py \
  --db data/production-cleanup-<ts>/production-working-<ts>.db \
  --out data/production-cleanup-<ts>/audit_report/
```
Outputs `supplier_quote_stats.csv` / `duplicate_clusters.csv` / `conservation_check.csv` / `canonical_suppliers.csv` (first draft).

**Step two: human fills in the merge plan**

Open `canonical_suppliers.csv` and fill the `human_verdict` column with `keep` / `merge_into:<id>` / `inactive`.
Export `merge_plan.csv` from it (only rows to be merged/deactivated) as the input to `merge_suppliers.py`.

Principles:
- string similarity alone must not auto-merge; there must be a business basis (same business license, same contact, known same entity)
- clusters with `conservation_ok=False` are forbidden from merging; the cause must be explained by a human before deciding

**Step three: dry-run-validate the merge plan**
```
python scripts/merge_suppliers.py \
  --merge-plan data/production-cleanup-<ts>/merge_plan.csv \
  --db data/production-cleanup-<ts>/production-working-<ts>.db \
  --dry-run
```
Confirm that the output's affected row counts match human expectations and `conservation_ok` is True everywhere.

**Step four: execute the merge (on the working copy)**
```
python scripts/merge_suppliers.py \
  --merge-plan data/production-cleanup-<ts>/merge_plan.csv \
  --db data/production-cleanup-<ts>/production-working-<ts>.db \
  --execute
```

**Step five: mark polluted Quotes and recompute reference prices**
```
python scripts/recalculate_ref_prices.py \
  --db data/production-cleanup-<ts>/production-working-<ts>.db \
  --out data/production-cleanup-<ts>/ref_price_changes.csv
```
Manually review `ref_price_changes.csv`; confirm the change cause is reasonable for materials with `needs_review=True`.

**Step six: output the before/after summary and conservation report**
```
python scripts/audit_suppliers.py \
  --db data/production-cleanup-<ts>/production-working-<ts>.db \
  --compare-baseline data/production-cleanup-<ts>/snapshot_manifest.json \
  --out data/production-cleanup-<ts>/audit_report/after/
```
Verify: Quote total row count unchanged, amounts conserved, project associations conserved, no dangling foreign keys.

**Step seven: create the cleaned copy and smoke-test**
```
cp data/production-cleanup-<ts>/production-working-<ts>.db \
   data/production-cleanup-<ts>/production-cleaned-<ts>.db
```
Mount `production-cleaned-<ts>.db` on a local application instance (`MEMPAS_DB_PATH` env var pointing to that file) and run these smoke tests:
- supplier list: shows only `merge_status='active'` suppliers, count as expected
- historical-quote statistics: `ref_price_median` matches the after value in `ref_price_changes.csv`
- bid-comparison flow: upload PDF → select supplier → stage for review → align → matrix → export (whole flow error-free)
- export Excel: historical matrix versions still download normally

---

### 7.4 Building the production write-back package

**Forbidden**: directly overwriting the production DB with the `production-cleaned.db` file.

Run the build script to generate the `fix_package/` directory:
```
python scripts/build_production_fix_package.py \
  --original data/production-cleanup-<ts>/production-original-<ts>.db \
  --cleaned  data/production-cleanup-<ts>/production-cleaned-<ts>.db \
  --manifest data/production-cleanup-<ts>/snapshot_manifest.json \
  --merge-plan data/production-cleanup-<ts>/merge_plan.csv \
  --out data/production-cleanup-<ts>/fix_package/
```

`fix_package/` contents:

| File | Notes |
|------|------|
| `apply_cleanup.sql` | parameterized SQL script, UPDATE/INSERT only (no DROP/DELETE), wrapped in a single transaction |
| `rollback_cleanup.sql` | rollback script (inverse operations, restore merge markers, restore supplier_id FKs) |
| `approved_merge_plan.json` | human-approved merge plan (with cluster_id, canonical_id, dup_ids, conservation_ok) |
| `approved_exclusions.json` | list of batch_ids approved for marking `excluded_from_ref` |
| `expected_before.json` | row-count/amount snapshot the production DB should have before execution (derived from snapshot_manifest.json) |
| `expected_after.json` | expected row-count/amount snapshot after execution (derived from the working copy's after report) |
| `SHA256SUMS` | SHA256 of all files (incl. apply_cleanup.sql and rollback_cleanup.sql) |
| `OPERATIONS.md` | execution manual (maintenance-window flow, per-step commands, validation methods, rollback triggers) |

`apply_cleanup.sql` structure sketch:
```sql
BEGIN TRANSACTION;

-- 1. foreign-key migration
UPDATE quotes SET supplier_id = :canonical_id WHERE supplier_id IN (:dup_ids);
UPDATE bid_submissions SET supplier_id = :canonical_id WHERE supplier_id IN (:dup_ids);
UPDATE bid_alignment_items SET supplier_id = :canonical_id WHERE supplier_id IN (:dup_ids);
UPDATE bid_invitations SET supplier_id = :canonical_id WHERE supplier_id IN (:dup_ids);

-- 2. alias migration
INSERT INTO supplier_aliases (...) VALUES (...) ON CONFLICT(...) DO NOTHING;

-- 3. mark merged/deactivated suppliers
UPDATE suppliers SET merge_status='merged', merged_into_supplier_id=:canonical_id
WHERE id IN (:dup_ids);

-- 4. mark polluted Quotes
UPDATE quotes SET bid_status='excluded_from_ref'
WHERE supplier_id IN (:inactive_ids);

-- 5. conservation validation (SELECT, produces no writes)
-- the script runs Python validation before COMMIT; ROLLBACK if it fails

COMMIT;
```

---

### 7.5 Formal production execution

**Precondition**: `fix_package/` has been reviewed and signed off by the owner (or approved via code review).

**Step one: maintenance window — pause writes**

Notify users of the maintenance window (off-peak recommended, reserve 30 minutes). On the production server:
```
# pause the application's write entry (frontend 503 or stop the app)
docker compose stop backend
```

**Step two: back up the production DB again**
```
sqlite3 /opt/mempas/data/mempas.db \
  ".backup /opt/mempas/data/mempas-pre-cleanup-<exec-timestamp>.db"
sha256sum /opt/mempas/data/mempas-pre-cleanup-<exec-timestamp>.db
```
This backup is the final rollback baseline, kept separately from the Phase 1 `.bak` file.

**Step three: verify production current state matches `expected_before.json`**
```
python scripts/verify_db_state.py \
  --db /opt/mempas/data/mempas.db \
  --expected data/production-cleanup-<ts>/fix_package/expected_before.json
```
Any field mismatch (row count, amount) → abort, re-run the §7.3 flow, do not force ahead.

**Step four: apply the fix in a single transaction**
```
python scripts/apply_cleanup_to_copy.py \
  --db /opt/mempas/data/mempas.db \
  --package data/production-cleanup-<ts>/fix_package/ \
  --execute
```
Inside the script:
1. `BEGIN TRANSACTION`
2. execute `apply_cleanup.sql` (parameterized, injecting parameters from `approved_merge_plan.json`)
3. run conservation validation (Quote row count, amount, project associations, FK integrity)
4. validation passes → `COMMIT`; any validation failure → `ROLLBACK`, output the failure cause, exit non-zero

**Step five: verify `expected_after.json`**
```
python scripts/verify_db_state.py \
  --db /opt/mempas/data/mempas.db \
  --expected data/production-cleanup-<ts>/fix_package/expected_after.json
```
Mismatch → execute rollback immediately (see below).

**Step six: restore the application and run smoke tests**
```
docker compose start backend
```
Run the same smoke-test checklist as §7.3 step seven (supplier list / quote history / full bid-comparison flow / export). Anomaly found → execute rollback.

**Rollback execution** (on any failure of step four/five/six):
```
# Method A: already ROLLBACK'd inside the transaction, DB unchanged, just restart the app
docker compose start backend

# Method B: DB state uncertain, restore from the pre-execution backup
docker compose stop backend
cp /opt/mempas/data/mempas-pre-cleanup-<exec-timestamp>.db \
   /opt/mempas/data/mempas.db
docker compose start backend
```

---

### 7.6 Tools delivered up front

The following tools are **implemented first** and must be ready before any production operation:

| Tool | Path | Function |
|------|------|------|
| `export_production_snapshot` | `scripts/export_production_snapshot.py` | retrieve a production-DB snapshot via SSH + backup API, compute SHA256, write `snapshot_manifest.json` |
| `audit_production_db.py` | `scripts/audit_suppliers.py` (with `--compare-baseline` mode) | generate the full audit-report suite (incl. before/after conservation comparison) |
| `apply_cleanup_to_copy.py` | `scripts/apply_cleanup_to_copy.py` | read the fix_package, execute on the target DB in a single transaction and validate |
| `build_production_fix_package.py` | `scripts/build_production_fix_package.py` | diff the original/cleaned copies, generate SQL, JSON, SHA256SUMS |
| `verify_db_state.py` | `scripts/verify_db_state.py` | compare actual DB state with the expected JSON, output differences |

Operations manual `docs/ops/production-cleanup-runbook.md` (new): covers the entire §7.1–§7.5 flow, including per-step commands, expected-output examples, abort conditions, and rollback-trigger criteria.

> _(corrected 2026-06-23: as of this audit, none of the §7.6 tools nor the §6.2/§6.3/§6.4 cleanup scripts exist — `scripts/` contains only `seed_supplier_aliases.py` from Phase 1. `audit_suppliers.py`, `merge_suppliers.py`, `recalculate_ref_prices.py`, `export_production_snapshot.py`, `apply_cleanup_to_copy.py`, `build_production_fix_package.py`, `verify_db_state.py`, and `docs/ops/production-cleanup-runbook.md` are all unbuilt. Section 7 (and the data-cleanup parts of §6) remains Planned, not scheduled.)_

---

## 8. Acceptance criteria (11 items)

All must pass before the Phase 2/3 migration task may be closed:

| # | Type | Acceptance content |
|---|------|---------|
| 1 | E2E | after uploading and staging three supplier quote PDFs (selecting existing suppliers via the frontend), the `suppliers` / `materials` / `quotes` row counts are completely unchanged; `bid_submissions` + `bid_quote_lines` have corresponding new records |
| 2 | E2E | a staged quote (any bid_status value) affects no material's `ref_price_*` fields and no supplier-recommendation score |
| 3 | E2E | without running `archive-prices`, alignment, review, matrix generation, and Excel export can still complete; export results match the manually-checked BidQuoteLine data |
| 4 | E2E | for the same quote fact, the matrices produced by the new path (BidQuoteLine) and the old path (historical Quote) agree on unit price, quantity, and amount for the corresponding rows |
| 5 | E2E | after running `archive-prices` the response contains `eligible_count / archived_count / skipped_count / skipped_lines`; rows with `material_id=NULL` appear in `skipped_lines`; `BidSubmission.status` accurately reflects the partial-success state (`partially_archived`) |
| 6 | unit | `_resolve_supplier()` returns the same `supplier_id` for both `"凯硕新正投标文件.pdf"` (filename alias, exact match after stripping) and `"凯硕新正（上海）机电设备科技发展有限公司"` (legal_name alias) |
| 7 | unit | `_resolve_supplier()` returns `{"error": "supplier_not_found"}` for a brand-new unknown name; the `suppliers` row count is unchanged |
| 8 | unit | `tender_pdf.extract_bidlist()` returns `detected_category="阀门"`; the frontend `tenderCategory` value is "阀门"; `material_class="水阀门"` appears only in the Step 2 UI display field and affects no query or assignment |
| 9 | migration validation | after Phase 1 completes, `bid_alignment_items` row count = pre-migration row count; all old `quote_id` FKs valid; the CHECK constraint passes for all old rows (all old rows are `quote_id IS NOT NULL AND bid_quote_line_id IS NULL`) |
| 10 | data cleanup | after `merge_suppliers.py --execute`, conservation_check re-validates: each merged cluster's Quote row count, total amount, and project_id associations are fully conserved; no `quotes.supplier_id` points at a `merge_status='merged'` supplier |
| 11 | data cleanup | after cleanup, `refresh_material_baselines()` recompute completes, outputting `ref_price_changes.csv`; every change record contains before/after values, sample counts, and exclusion reasons; materials with `needs_review=True` (change magnitude > ±30%) are closed after human confirmation |

---

## 9. List of modules to implement

| Module | File (estimated) | Phase |
|------|------------|------|
| DDL: 3 new tables + BidAlignmentItem rebuild | `database.py` / migration script | Phase 1 |
| Alias seed-data migration | `scripts/seed_supplier_aliases.py` | Phase 1 |
| `suppliers` two-column ADD COLUMN | `database.py` | Phase 1 |
| `quote_filters.py` (valid-historical-quote query) | `apps/api/services/history/quote_filters.py` (new) | Phase 2/3 |
| `_resolve_supplier()` 7-layer lookup | `apps/api/services/supplier/supplier_resolve.py` (new) | Phase 2/3 |
| `batch-confirm` rework (write the staging layer) | `apps/api/routes/quotes.py` | Phase 2/3 |
| `archive-prices` endpoint | `apps/api/routes/quotes.py` | Phase 2/3 |
| `tender_pdf.py` `detected_category` | `apps/api/services/tender/tender_pdf.py` | Phase 2/3 |
| `match` reads BidQuoteLine | `apps/api/routes/analysis.py` | Phase 2/3 |
| matrix/review/export fallback compatibility | `apps/api/services/matrix/bid_matrix.py` / `routes/export.py` | Phase 2/3 |
| `refresh_material_baselines` uses `quote_filters` | `apps/api/services/` | Phase 2/3 |
| supplier-recommendation scoring uses `quote_filters` | `apps/api/services/` | Phase 2/3 |
| frontend supplier-selection UX | `apps/www/src/views/compare/IndexView.vue` | Phase 2/3 |
| frontend `tenderCategory` fix | `apps/www/src/views/compare/IndexView.vue` | Phase 2/3 |
| alias-management UI | supplier-management page | Phase 2/3 |
| `audit_suppliers.py` | `scripts/` | Phase 4 |
| `merge_suppliers.py` | `scripts/` | Phase 4 |
| `recalculate_ref_prices.py` | `scripts/` | Phase 4 |
| `export_production_snapshot.py` | `scripts/` | Phase 4 (delivered up front) |
| `apply_cleanup_to_copy.py` | `scripts/` | Phase 4 (delivered up front) |
| `build_production_fix_package.py` | `scripts/` | Phase 4 (delivered up front) |
| `verify_db_state.py` | `scripts/` | Phase 4 (delivered up front) |
| `seed_supplier_aliases.py` | `scripts/` | Phase 1 |
| Production-cleanup operations manual | `docs/ops/production-cleanup-runbook.md` | Phase 4 (delivered up front) |

> _(corrected 2026-06-23: of this list, the Phase 1 DDL/seed work and all Phase 2/3 backend+frontend modules are implemented (with the resolver named `resolve_supplier()` and the filter named `valid_quote_filters()`). Every Phase 4 row remains unbuilt except `seed_supplier_aliases.py` (which is actually Phase 1). The alias-management UI was not verified in this audit.)_
