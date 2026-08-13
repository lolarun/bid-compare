# MEMPAS Technical Design v2

> **Status — audited 2026-06-23.** Partially implemented / partially superseded by `09-P0数据模型与写链路重构.md`, `12-招标比价后端审计与整改.md`, and `13-Alembic迁移引入.md`. The high-level tech stack and Vue/FastAPI layering still hold, but the data model, write path, recognition pipeline, alignment endpoints, scoring weights, and migration strategy have all moved on substantially since this was written.
> _Originally written May 2026 (v2 dated 2025-05-19, last revised v2.2 2026-05-26). English translation of the Chinese original; now the authoritative version._

> Electromechanical-materials lookup and bid-comparison analysis system — technical architecture and implementation plan (revised against 2025-05-19 user feedback)

## Revision history

| Version | Date | Change notes |
|------|------|---------|
| v1 | 2025-05-18 | Initial technical design |
| v2 | 2025-05-19 | Project structure reorganized into apps/api + apps/www; bid-comparison algorithm switched to a reasonable historical-low basis; corrected Z-score removed; brand-tier model added; switchgear-cabinet split down to the component level |
| v2.1 | 2026-05-19 | All P0/P1 implemented; added brand_tier model/route, bid_matrix service, auth route, dashboard heatmap/bubble endpoints; scoring/import/comparison fully rewritten; 101 test cases passing; version 0.2.0 |
| v2.2 | 2026-05-26 | Design adds the "AI alignment review of multiple supplier quotes for the same tender" flow: an LLM analyzes unaligned quotes and suspected field misalignment, and the user confirms before the final comparison matrix is produced |

---

## 1. Tech stack

> Superseded in part: the recognition stack below is incomplete. Current code adds a two-stage DashScope OCR + text-LLM pipeline (`apps/api/intelligence/`, provider `DashScopeOCRProvider`) plus an Alembic migration toolchain (see `13-Alembic迁移引入.md`).

| Layer | Technology | Version | Notes |
|----|------|------|------|
| Backend framework | FastAPI | ≥0.115 | Async Python web framework |
| ORM | SQLAlchemy | ≥2.0 | Database mapping |
| Database | SQLite | 3.x | WAL mode, single-node deployment |
| Data validation | Pydantic | ≥2.10 | Request/response schemas |
| Data analysis | pandas + numpy | — | Statistical computation |
| Package manager | uv | — | Python dependency management |
| Frontend framework | Vue 3 | ≥3.5 | Composition API + `<script setup>` |
| Build tool | Vite | ≥8.0 | TypeScript support |
| UI library | Ant Design Vue | 4.x | Enterprise component library |
| State management | Pinia | ≥2.0 | Persistence plugin |
| HTTP client | Axios | ≥1.7 | API requests + interceptors |
| Backend testing | pytest | ≥8.0 | 101 test cases *(corrected 2026-06-23: the suite is now far larger and spans `apps/api/tests` plus `tests`; the "101" figure is stale.)* |
| Frontend testing | Vitest | ≥4.0 | 29 test cases *(corrected 2026-06-23: count is stale.)* |

## 2. Project structure

> Superseded in part: the actual `apps/api/` tree now also contains `intelligence/` (OCR / page-role classification / table extraction / LLM pipeline) and `migrations/` (Alembic), and the `services/`, `models/`, `schemas/`, and `routes/` packages hold many modules not listed below (e.g. `services/alignment/bid_alignment.py`, `services/tender/tender_list.py`, `services/matrix/matrix_stats.py`; `models/bid_submission.py`, `models/extraction_job.py`, `models/tender_list_session.py`, `models/bid_matrix_version.py`; `routes/intake.py`, `routes/invite.py`, `routes/export.py`, `routes/users.py`, `routes/logs.py`). The skeleton below reflects the May-2026 design intent, not the current file list.

```
bid-compare/
├── apps/
│   ├── api/                          # Backend (FastAPI)
│   │   ├── main.py                   # Entry point + SPA static-file serving
│   │   ├── core/
│   │   │   ├── config.py             # Config constants + extended-attribute schema
│   │   │   └── database.py           # SQLAlchemy engine + Session
│   │   ├── models/                   # Data models
│   │   │   ├── material.py
│   │   │   ├── supplier.py
│   │   │   ├── project.py
│   │   │   ├── quote.py
│   │   │   ├── brand_tier.py         # Brand-tier mapping table (added in v2.1)
│   │   │   └── analysis_config.py
│   │   ├── schemas/                  # Pydantic schemas
│   │   │   ├── material.py
│   │   │   ├── supplier.py
│   │   │   ├── project.py
│   │   │   ├── quote.py
│   │   │   ├── analysis.py           # BidMatrix/BrandTier/Dashboard visualization schemas (extended in v2.1)
│   │   │   └── common.py
│   │   ├── services/                 # Business-logic layer
│   │   │   ├── comparison.py         # Bid-comparison algorithm (reasonable historical-low basis, rewritten in v2.1)
│   │   │   ├── scoring.py            # Supplier scoring (short-key weights + brand-tier hit rate, rewritten in v2.1)
│   │   │   ├── statistics.py         # Category statistics + dashboard heatmap/bubble (extended in v2.1)
│   │   │   ├── bid_matrix.py         # Horizontal comparison matrix (added in v2.1)
│   │   │   ├── import_service.py     # Excel/CSV import (material dedup + supplier separation, rewritten in v2.1)
│   │   │   └── standardize.py        # Material-name standardization
│   │   └── routes/                   # API routes
│   │       ├── __init__.py           # Route aggregation
│   │       ├── materials.py
│   │       ├── suppliers.py
│   │       ├── projects.py
│   │       ├── quotes.py
│   │       ├── analysis.py           # bid-matrix + heatmap/bubble (extended in v2.1)
│   │       ├── brand_tiers.py        # Brand-tier CRUD (added in v2.1)
│   │       ├── auth.py               # JWT login (added in v2.1)
│   │       └── config.py
│   └── www/                          # Frontend (Vue 3)
│       ├── src/
│       │   ├── api/                  # API client + TypeScript types
│       │   │   ├── client.ts         # Axios instance + interceptors + type definitions
│       │   │   └── index.ts          # API method collection
│       │   ├── assets/               # Static assets (logo, etc.)
│       │   ├── layouts/
│       │   │   └── BasicLayout.vue   # Main layout (fixed sidebar + top bar + content area)
│       │   ├── views/
│       │   │   ├── dashboard/IndexView.vue   # Workbench (summary cards + trends + heatmap + bubble chart)
│       │   │   ├── compare/IndexView.vue     # Tender comparison (horizontal comparison matrix)
│       │   │   ├── invite/IndexView.vue      # Bid-invitation suggestions
│       │   │   ├── materials/IndexView.vue   # Material master data
│       │   │   ├── history/IndexView.vue     # Historical procurement prices
│       │   │   ├── suppliers/IndexView.vue   # Quality suppliers
│       │   │   ├── import/IndexView.vue      # Price import (Excel + OCR tab)
│       │   │   ├── system/UsersView.vue      # User management (frontend done, backend not yet implemented)
│       │   │   ├── system/LogsView.vue       # Operation logs (frontend done, backend not yet implemented)
│       │   │   ├── system/SettingsView.vue   # System settings
│       │   │   ├── login/LoginView.vue
│       │   │   └── exception/404.vue
│       │   ├── router/index.ts       # Routing + NProgress + login guard
│       │   ├── stores/
│       │   │   ├── app.ts            # Global state (sidebar collapse, dashboard data)
│       │   │   └── user.ts           # User authentication state
│       │   ├── styles/global.css     # Global styles + CSS variables
│       │   ├── App.vue               # ConfigProvider + theme
│       │   ├── main.ts               # Entry point
│       │   └── __tests__/            # Frontend tests
│       └── vite.config.ts            # Vite + dev proxy + @ alias
├── tests/                            # Backend tests (101 cases)
│   ├── conftest.py
│   ├── test_models.py
│   ├── test_materials.py
│   ├── test_suppliers.py
│   ├── test_projects.py
│   ├── test_quotes.py
│   ├── test_analysis.py
│   ├── test_import.py
│   └── test_config.py
├── scripts/                          # Standalone scripts
│   ├── analyze_data.py               # Data analysis + Markdown report
│   ├── export_excel.py               # Excel report generation
│   ├── import_data.py                # CSV → SQLite initial import
│   └── convert_excel_to_csv.py       # Raw Excel → CSV preprocessing
├── data/                             # Runtime data
│   └── mempas.db                     # SQLite database
├── docs/                             # Documentation
├── pyproject.toml                    # Python dependencies (uv)
└── uv.lock
```

> Note (corrected 2026-06-23): `system/UsersView.vue` and `system/LogsView.vue` are annotated above as "backend not yet implemented." Both backends now exist — see `apps/api/routes/users.py` and `apps/api/routes/logs.py`, registered in `routes/__init__.py`.

## 3. Database design

> Superseded in part by `09-P0数据模型与写链路重构.md` and `12-招标比价后端审计与整改.md`. The recognition write path no longer flows quotes straight into `quotes`; recognition output lands in an `ExtractionDraft`/`ExtractionJob` staging path and becomes an official quote fact only after user confirmation, and an anchor-centric model (`TenderListSession`, `BidSubmission`/`BidQuoteLine`, `BidAlignmentGroup`/`BidAlignmentItem`, `BidMatrixVersion`, `AlignmentFinalization`, `SupplierAlias`, `User`, `OperationLog`) now sits alongside the tables described here. Treat the tables below as the v2 baseline only.

### 3.1 ER relationships

```
materials 1 ←──→ N quotes
suppliers 1 ←──→ N quotes
projects 1 ←──→ N quotes
brand_tiers — standalone mapping table
analysis_config — key-value table
```

### 3.2 Table schemas

#### materials — material master data

| Column | Type | Constraint | Notes |
|----|------|------|------|
| id | INTEGER | PK, AUTO | Primary key |
| material_code | VARCHAR(50) | UNIQUE | Material code (nullable, pending the first-construction party's own scheme) |
| standard_name | VARCHAR(200) | NOT NULL | Standard name |
| profession | VARCHAR(20) | NOT NULL, INDEX | Profession: electrical / plumbing / HVAC |
| category | VARCHAR(20) | NOT NULL, INDEX | Category |
| sub_category | VARCHAR(40) | | Sub-category |
| spec | VARCHAR(200) | | Specification / model |
| material_type | VARCHAR(100) | | Material type |
| unit | VARCHAR(10) | | Unit of measure |
| brand | VARCHAR(100) | | Brand |
| exec_standard | VARCHAR(100) | | Standard followed |
| extended_attrs | JSON | DEFAULT {} | Layer 2 extended attributes |
| ref_price_low | FLOAT | | Reasonable historical low |
| ref_price_avg | FLOAT | | IQR-filtered mean price |
| ref_price_median | FLOAT | | Median price |
| ref_price_high | FLOAT | | P90 |
| price_cv | FLOAT | | Coefficient of variation |
| deviation_threshold | FLOAT | | Deviation tolerance |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

Composite index: `(profession, category)`

*(corrected 2026-06-23: the live `materials` table also carries a `status VARCHAR(16) NOT NULL DEFAULT 'active'` column with index `ix_materials_status`, added during the soft-delete/governance work — see `apps/api/core/database.py`.)*

#### suppliers — suppliers

| Column | Type | Constraint | Notes |
|----|------|------|------|
| id | INTEGER | PK, AUTO | |
| name | VARCHAR(200) | UNIQUE, NOT NULL | |
| short_name | VARCHAR(50) | | Short name |
| contact | VARCHAR(100) | | Contact person |
| phone | VARCHAR(30) | | Phone |
| categories | JSON | DEFAULT [] | Business categories |
| win_count | INTEGER | DEFAULT 0 | Award count |
| cooperation_score | FLOAT | DEFAULT 0 | Commercial-terms score |
| remark | TEXT | | |

*(corrected 2026-06-23: the live `suppliers` table also carries supplier-cleanup columns `merge_status VARCHAR(20) NOT NULL DEFAULT 'active'` (index `ix_suppliers_merge_status`) and `merged_into_supplier_id INTEGER REFERENCES suppliers(id)`, plus a separate `supplier_aliases` table — see `09-P0数据模型与写链路重构.md`.)*

#### projects — projects

| Column | Type | Constraint | Notes |
|----|------|------|------|
| id | INTEGER | PK, AUTO | |
| name | VARCHAR(200) | NOT NULL | |
| code | VARCHAR(50) | | Project code |
| location | VARCHAR(200) | | Location |
| status | VARCHAR(20) | DEFAULT 'in progress' | |
| remark | TEXT | | |

#### quotes — quote records

| Column | Type | Constraint | Notes |
|----|------|------|------|
| id | INTEGER | PK, AUTO | |
| material_id | INTEGER | FK → materials.id | |
| supplier_id | INTEGER | FK → suppliers.id | |
| project_id | INTEGER | FK → projects.id | |
| unit_price | FLOAT | | Tax-inclusive unit price |
| unit_price_excl_tax | FLOAT | | Tax-exclusive unit price |
| tax_rate | FLOAT | | Tax rate |
| quantity | FLOAT | | Quantity |
| total_price | FLOAT | | Total price |
| brand | VARCHAR(100) | | Brand |
| brand_tier | VARCHAR(20) | | Brand tier (tier 1 / tier 2 / tier 3) |
| remark | TEXT | | Original remark text (payment / warranty / technical info; display-only, not used in comparison) |
| quote_date | VARCHAR(20) | | Quote date |
| batch_id | VARCHAR(50) | | Import batch ID |
| deviation_pct | FLOAT | | Deviation rate (vs reasonable historical low) |
| alert_level | VARCHAR(10) | | normal/yellow/red |

Index: `(material_id, supplier_id)`

*(corrected 2026-06-23: the live `quotes` table also carries `extraction_meta_json JSON` (row-level extraction evidence for LLM supplier-fill). Confirmed quotes now originate from the `BidSubmission`/`BidQuoteLine` write path, not direct inserts — see `09-P0数据模型与写链路重构.md`.)*

#### brand_tiers — brand-tier mapping (new)

| Column | Type | Constraint | Notes |
|----|------|------|------|
| id | INTEGER | PK, AUTO | |
| brand_name | VARCHAR(100) | UNIQUE, NOT NULL | Brand name |
| tier | VARCHAR(20) | NOT NULL | tier 1 / tier 2 / tier 3 |
| category | VARCHAR(20) | | Owning category (nullable = generic) |
| created_at | DATETIME | | |
| updated_at | DATETIME | | |

#### analysis_config — system configuration

| key | Contents |
|-----|------|
| `scoring_weights` | `{price: 0.40, history: 0.20, completeness: 0.15, brand: 0.15, commercial: 0.10}` *(corrected 2026-06-23: the live defaults use long keys and dropped the brand dimension on 2026-06-06 — `{price_competitiveness: 0.45, history_cooperation: 0.25, quote_completeness: 0.15, commercial_terms: 0.15}`. Manual bid-comparison never scores by brand tier, so the brand 0.15 was redistributed to price/history/commercial. See `apps/api/core/config.py` `DEFAULT_SCORING_WEIGHTS`.)* |
| `thresholds` | `{default: {yellow: 0.05, red: 0.10}, 桥架: {yellow: 0.08, red: 0.15}, ...}` |
| `extended_attr_schemas` | Per-category extended-attribute schemas |

## 4. API design

### 4.1 Endpoint overview

> Superseded in part by `12-招标比价后端审计与整改.md`: `routes/analysis.py` now exposes a far larger anchor-centric surface than the table below — including the full `tender-list/*` flow (`preview`, `reconcile`, `match`, `llm-fill`, `confirm`, `current`, `current-sessions`, `versions`), `anchor-review/*` (`matrix`, `item-confirm`, `confirm`, `bulk-confirm`, `finalize`), `bid-matrix/save` + `bid-matrix/versions/*`, `bid-insight`, and `bid-alignment/groups` CRUD. The two `bid-alignment` endpoints below marked "to be implemented" are now implemented.

| Method | Path | Function |
|------|------|------|
| **Materials** | | |
| GET | `/api/materials` | List (pagination + filter + search) |
| GET | `/api/materials/{id}` | Detail |
| POST | `/api/materials` | Create |
| PUT | `/api/materials/{id}` | Update |
| DELETE | `/api/materials/{id}` | Delete |
| GET | `/api/materials/categories` | Category statistics |
| POST | `/api/materials/standardize` | Name standardization |
| GET | `/api/materials/extended-schema/{category}` | Extended-attribute schema |
| **Suppliers** | | |
| GET/POST/PUT/DELETE | `/api/suppliers[/{id}]` | CRUD |
| **Projects** | | |
| GET/POST/PUT/DELETE | `/api/projects[/{id}]` | CRUD |
| **Quotes** | | |
| GET/POST/PUT/DELETE | `/api/quotes[/{id}]` | CRUD (deviation auto-computed on create) |
| POST | `/api/quotes/import` | Batch import (returns `unknown_brands` for a frontend popup) |
| POST | `/api/quotes/ocr` | OCR scanned-document parsing (route registered, parsing logic not yet wired) *(corrected 2026-06-23: there is no `/api/quotes/ocr` route in the current code. Document recognition moved to `routes/intake.py` plus the two-stage `apps/api/intelligence/` pipeline (DashScope OCR + text LLM).)* |
| POST | `/api/quotes/ocr/confirm` | Store confirmed OCR results *(corrected 2026-06-23: not present; superseded by the intake/confirm flow.)* |
| GET | `/api/quotes/stats` | Quote statistics |
| **Analysis** | | |
| POST | `/api/analysis/compare` | Single-item comparison (reasonable historical-low basis, no baseline_type) |
| POST | `/api/analysis/supplier-score` | Supplier scoring (5 dimensions, short-key weights) *(corrected 2026-06-23: now 4 dimensions; brand removed 2026-06-06.)* |
| POST | `/api/analysis/multi-compare` | Multi-supplier comparison |
| GET | `/api/analysis/dashboard` | Workbench summary statistics |
| GET | `/api/analysis/dashboard/heatmap` | Tree heatmap data (project → category → amount) |
| GET | `/api/analysis/dashboard/bubble` | Bubble-chart data (category → supplier → amount) |
| GET | `/api/analysis/category-stats/{category}` | Category price distribution |
| POST | `/api/analysis/refresh-baselines` | Refresh all material baselines (category optional) |
| POST | `/api/analysis/bid-matrix` | Horizontal comparison matrix |
| POST | `/api/analysis/bid-alignment/suggest` | AI quote-alignment / field-correction suggestions (to be implemented) *(corrected 2026-06-23: implemented — see `routes/analysis.py` and `services/alignment/bid_alignment.py`.)* |
| POST | `/api/analysis/bid-alignment/apply` | Apply the confirmed alignment plan and produce the final matrix (to be implemented) *(corrected 2026-06-23: implemented.)* |
| **Brand tiers** | | |
| GET | `/api/brand-tiers` | Tier list |
| POST | `/api/brand-tiers` | Create tier |
| GET | `/api/brand-tiers/unknown` | List of unassigned brands |
| GET/PUT/DELETE | `/api/brand-tiers/{id}` | Detail / update / delete |
| **Authentication** | | |
| POST | `/api/auth/login` | User login (returns JWT token; env: JWT_SECRET, ADMIN_USER, ADMIN_PASS) *(corrected 2026-06-23: login now validates against a real `users` table and seeds a default admin from ADMIN_USER/ADMIN_PASS only when the table is empty; it is no longer an env-only credential check. See `routes/auth.py`.)* |
| **Configuration** | | |
| GET | `/api/config` | Config list |
| GET | `/api/config/{key}` | Get config |
| PUT | `/api/config/{key}` | Update config (weights/thresholds validated) |

### 4.2 Key endpoint changes

#### POST /api/analysis/compare — single-item comparison (modified)

Request:
```json
{
  "category": "桥架",
  "sub_category": "托盘式桥架",
  "new_price": 48.0
}
```

Response:
```json
{
  "category": "桥架",
  "sub_category": "托盘式桥架",
  "reasonable_low": 42.0,
  "reasonable_low_project": "华泾镇D5B",
  "reasonable_low_date": "2024-06",
  "baseline_avg": 52.3,
  "baseline_median": 50.5,
  "historical_min": 38.5,
  "baseline_high": 62.0,
  "new_price": 48.0,
  "deviation_pct": 0.143,
  "alert_level": "red",
  "sample_count": 156
}
```

#### POST /api/analysis/bid-matrix — horizontal comparison matrix (new)

Request:
```json
{
  "project_id": 5,
  "category": "给排水",
  "supplier_ids": [1, 2, 3, 4]
}
```

Response:
```json
{
  "summary": {
    "total_materials": 5,
    "total_suppliers": 4,
    "recommended_supplier": {"id": 3, "name": "上海管业"},
    "optimal_total": 53900.00,
    "anomaly_count": 1
  },
  "rows": [
    {
      "material_id": 101,
      "material_name": "DN100 无缝钢管",
      "spec": "Q235 · 200 米",
      "historical_avg": {"price": 72.00, "period": "2023-01~2024-12", "projects": 5},
      "reasonable_low": {"price": 65.00, "date": "2024-03", "project": "华泾镇D5B"},
      "suppliers": [
        {
          "supplier_id": 1,
          "price": 78.00,
          "total": 15600,
          "deviation_pct": 0.20,
          "alert_level": "red",
          "is_lowest": false
        },
        {
          "supplier_id": 3,
          "price": 70.00,
          "total": 14000,
          "deviation_pct": 0.077,
          "alert_level": "yellow",
          "is_lowest": true
        }
      ],
      "min_deviation": -0.028,
      "recommended": "C"
    }
  ],
  "totals": [
    {"supplier_id": 1, "total": 58480, "avg_deviation": 0.049},
    {"supplier_id": 3, "total": 46100, "avg_deviation": -0.037}
  ]
}
```

## 5. Core algorithms

### 5.1 Reasonable historical-low calculation (IQR method, modified)

```
Input: all historical quotes prices[] for a given category/sub-category + matching condition
Steps:
  1. Q1 = percentile(prices, 25)
  2. Q3 = percentile(prices, 75)
  3. IQR = Q3 - Q1
  4. lower = Q1 - 1.5 × IQR
  5. upper = Q3 + 1.5 × IQR
  6. filtered = prices[lower <= p <= upper]
  7. reasonable_low = min(filtered)         ← reasonable historical low (primary basis)
  8. baseline_avg = mean(filtered)          ← historical mean (auxiliary)
  9. baseline_median = median(filtered)     ← median (auxiliary)
  10. historical_min = min(prices)           ← absolute minimum (alert only)
Output: reasonable_low, avg, median, std, cv, p10, p90, min, max
```

### 5.2 Deviation rate and color-flag determination (modified)

```
deviation_pct = (new_price - reasonable_low) / reasonable_low

Color flag (thresholds configurable per category):
  |deviation| ≤ yellow_threshold  → "normal" (no color)
  yellow_threshold < |deviation| ≤ red_threshold → "yellow" (attention needed)
  |deviation| > red_threshold → "red" (anomaly)

Default thresholds: yellow = 5%, red = 10%
```

The green/blue alert scheme is no longer used, and the corrected Z-score is no longer used.

### 5.3 Horizontal comparison-matrix algorithm (new)

> Superseded in part by `12-招标比价后端审计与整改.md` and the anchor-centric rework: the matrix is now built on the `TenderAnchor`/`TenderListSession` row axis with `BidSubmission.id` as column identity and an `AlignmentFinalization`/`BidMatrixVersion` flow, rather than aggregating directly off `quotes.material_id`. The pseudocode below records the v2 intent.

```
Input: project_id, category, supplier_ids[]
Steps:
  1. Pull all quotes for this project + category from the quotes table
  2. Prefer a "user-confirmed alignment plan" to build comparison rows; with no alignment plan, build the initial matrix by material_id
  3. For each comparison row:
     a. Query historical data by matching condition (extended-attribute fields with role="match" in the category)
     b. Compute reasonable_low (with its source project and date)
     c. Compute each supplier's deviation_pct = (price - reasonable_low) / reasonable_low
     d. Mark is_lowest, alert_level
     e. Recommendation = supplier with the smallest deviation
  4. Bottom summary: each supplier's total quoted-material price, average deviation
  5. Recommended primary supplier = highest total score (price + completeness + brand-compliance combined)
```

### 5.4 AI quote-alignment review algorithm (in design)

> Superseded in part: this "in design" section is now built. See `routes/analysis.py` (`bid-alignment/suggest`, `bid-alignment/apply`, `bid-alignment/groups`), `services/alignment/bid_alignment.py`, and the `BidAlignmentGroup`/`BidAlignmentItem` models. The persisted shape evolved during `09-P0数据模型与写链路重构.md` (e.g. `bid_alignment_items` carries both `quote_id` and `bid_quote_line_id` with a mutual-exclusion CHECK, plus `submission_id`).

#### Background

Multiple bid documents for the same tender usually derive from a single procurement list, but the supplier quote PDFs do not express them consistently:

- The same list item may be recognized under different names: e.g. `Y型过滤器`, `不锈钢Y型过滤器`, `给排水 Y型过滤器`.
- The same list item's model may be split across different fields: e.g. `DN20` vs `DN20 PN16YZ`.
- The model may recognize a "line total" as a "unit price," producing anomalously high amounts.
- Relying solely on a fixed word list or hard-coded rules under-covers cases and easily merges distinct list items by mistake.

So this capability does not auto-edit the database directly; it operates as an "AI suggestion + user confirmation" review flow.

#### Trigger conditions

AI review fires during the comparison flow when any of the following holds:

1. Suppliers under the same project ≥ 2, and the matrix has many "not quoted" cells.
2. Several material names/specs are similar but scattered across different material_id values.
3. `unit_price × quantity` deviates from `total_price` beyond a threshold (default 1%), suggesting a column misalignment.
4. One supplier's quote completeness is clearly lower than the others while the source files have a similar row count.

#### Input data

`POST /api/analysis/bid-alignment/suggest`

```json
{
  "project_id": 59,
  "category": "阀门",
  "supplier_ids": [7, 58, 59],
  "rows": [
    {
      "quote_id": 11986,
      "supplier_name": "上海泰科龙",
      "material_name": "Y型过滤器",
      "spec": "DN20",
      "unit": "个",
      "quantity": 1,
      "unit_price": 69.12,
      "total_price": 69.12,
      "source_file": "泰科龙投标文件.pdf",
      "source_page": 1,
      "source_row": 3
    }
  ]
}
```

The data source is primarily `quotes + materials + suppliers + extraction_jobs.result`; later it should be supplemented with source metadata (file name, page number, row number, original OCR text fragment) so the user can cross-check.

#### LLM task

The model only outputs "suggestions" and must not write to the database directly:

1. Identify quote groups suspected to be the same tender list item.
2. Suggest a standard row name, standard spec, unit, and quantity for each group.
3. Mark each supplier's corresponding quote_id.
4. Flag field-correction suggestions, e.g. "unit_price may need to be total_price / quantity."
5. Give a confidence and a reason; the reason must cite checkable fields (name, spec, quantity, unit, price, adjacent-row order).

Output example:

```json
{
  "groups": [
    {
      "suggested_name": "Y型过滤器",
      "suggested_spec": "DN20",
      "confidence": 0.92,
      "reason": "三条记录名称均包含 Y型过滤器，规格均包含 DN20，单位均为个，数量均为1，位于各文件清单首组。",
      "items": [
        {"quote_id": 11986, "supplier_id": 7, "action": "align"},
        {"quote_id": 12052, "supplier_id": 58, "action": "align", "spec_note": "PN16YZ 作为型号差异保留"},
        {"quote_id": 12129, "supplier_id": 59, "action": "align", "name_note": "不锈钢作为材质差异保留"}
      ],
      "field_fixes": []
    }
  ],
  "field_fixes": [
    {
      "quote_id": 12053,
      "field": "unit_price",
      "current": 1802,
      "suggested": 106,
      "confidence": 0.86,
      "reason": "数量为17，total_price 为1802；当前 unit_price 等于合价，疑似列错位。"
    }
  ]
}
```

#### User confirmation and persistence

The frontend shows a "pending alignment suggestions" panel:

- The left side shows the original recognized rows: supplier, file, page number, original name, spec, quantity, unit price, line total.
- The right side shows the AI suggestions: standard row name, standard spec, merge group, field corrections.
- The user can act per group: confirm, reject, split, manually adjust.
- Only after the user confirms is `POST /api/analysis/bid-alignment/apply` called.

Backend persistence recommendations:

- Add `bid_alignment_groups`: store the standard rows the user confirmed within one comparison task.
- Add `bid_alignment_items`: store the quote_id → alignment_group_id mapping, plus field-correction override values.
- Do not overwrite the original recognized values in `materials` and `quotes`; preserve traceability.
- `bid_matrix` aggregates by alignment_group_id first; unconfirmed rows still display by material_id.

#### Constraints

- Below the confidence threshold (suggested 0.75), do not pre-check by default.
- When quantity or unit clearly conflict, the user must be prompted; do not auto-merge.
- Differences in material type, model, brand, etc. are kept as "difference notes" and are not blocking by default; whether they block is decided by the category-attribute configuration.
- All AI suggestions must be reversible and must keep an operation log.

### 5.5 Supplier scoring

> Superseded in part (corrected 2026-06-23): the `brand` dimension was removed on 2026-06-06 and its 0.15 weight redistributed; live weights are price_competitiveness 0.45 / history_cooperation 0.25 / quote_completeness 0.15 / commercial_terms 0.15. The dimension formulas below otherwise still describe the intent.

```
total = Σ(dimension_score × weight)

dimensions:
  price:        based on average deviation rate (vs reasonable historical low), linearly mapped to [20, 100]
  history:      win_count >= 5 → 100, >= 3 → 80, >= 1 → 60, 0 → 40
  completeness: valid_quotes / total_quotes × 100
  brand:        brand-tier hit rate × 100
  commercial:   manually entered (default 60)
```

### 5.6 Matching-condition grouping

During comparison, quotes are grouped by the extended-attribute fields with `role = "match"` in the category; only quotes whose match fields are all identical are compared together. Fields with `role = "difference"` may differ and are explained as remarks in the report.

### 5.7 Switchgear-cabinet special handling

Switchgear cabinets are not compared as whole cabinets; they are split to the component level:
- On import, parse the BOM structure and generate an independent material + quote record per component
- Matching condition: component name + spec + brand series
- Difference field: the component's specific model

## 6. Frontend architecture

### 6.1 Layout system

```
App.vue (ConfigProvider + theme)
├── /login → login/LoginView.vue (standalone layout, gradient background)
├── / → BasicLayout.vue (main layout)
│   ├── Fixed sidebar (logo + grouped navigation menu, collapsible)
│   ├── Top bar (breadcrumb + user avatar dropdown)
│   └── Content area (route-level transition animation)
│       ├── /dashboard  → dashboard/IndexView.vue   Workbench (F1)
│       ├── /compare    → compare/IndexView.vue     Tender comparison matrix (F6.1)
│       ├── /invite     → invite/IndexView.vue      Bid-invitation suggestions
│       ├── /materials  → materials/IndexView.vue   Material master data (F2)
│       ├── /history    → history/IndexView.vue     Historical procurement prices
│       ├── /suppliers  → suppliers/IndexView.vue   Suppliers (F3)
│       ├── /import     → import/IndexView.vue      Price import (F8)
│       ├── /system/users    → system/UsersView.vue
│       ├── /system/logs     → system/LogsView.vue
│       ├── /system/settings → system/SettingsView.vue (F7)
│       └── Compatibility redirects: /projects→/compare, /quotes→/history, /analysis→/compare, /settings→/system/settings
└── /* → exception/404.vue
```

### 6.2 Authentication flow

```
1. Route guard checks mempas_token in localStorage
2. No token → redirect to /login?redirect=<original path>
3. Login success → store token + userInfo → navigate to redirect
4. API interceptor: requests automatically carry the Bearer token
5. 401 response → clear token → navigate to /login
```

### 6.3 Theme configuration

```typescript
{
  token: { colorPrimary: '#1677ff', borderRadius: 6 },
  algorithm: theme.defaultAlgorithm,
  locale: zhCN
}
```

### 6.4 Path aliases

- Vite: `resolve.alias: { '@': resolve(__dirname, 'src') }`
- TypeScript: `paths: { "@/*": ["./src/*"] }`
- All imports uniformly use `@/api`, `@/stores`, `@/layouts`, etc.

## 7. Deployment plan

### 7.1 Development environment

> Note (corrected 2026-06-23): `--reload` is now prohibited per `CLAUDE.md` §3; on a port conflict, kill the existing process and restart on the same fixed port (backend 8000, frontend 3000). The command below retains `--reload` as written in the v2 original.

```bash
# Backend
cd bid-compare
uv run uvicorn apps.api.main:app --reload --port 8000

# Frontend (dev mode, auto-proxies /api → :8000)
cd apps/www
npm run dev  # → http://localhost:3000
```

### 7.2 Production environment (Alibaba Cloud ECS single-node deployment)

**Target user scale**: about 20 online users, 1–3 OCR/min

**Recommended configuration**:

| Item | Spec | Notes |
|------|------|------|
| Instance | ecs.c9i.large 2-core (vCPU) 4GiB, compute-type c9i | LLM inference goes through the DashScope API; the local node only forwards IO |
| System disk | ESSD Entry 40GiB | Stores uploaded files + SQLite + Docker images; expand to 60G if insufficient |
| Billing | Subscription (yearly, 27% off for 1 year) | About ¥173/month (¥2,072/year) |

**ECS instance info**:

| Item | Value |
|------|------|
| Public IP | 101.37.166.68 |
| Login user | root |
| Login password | (see the deployment engineer) |
| OS | To be confirmed |

**Deployment method**: Docker Compose with two containers (nginx + uvicorn); see `docs/DEPLOY.md` for details

```bash
# Build the frontend
cd apps/www && npm run build

# Start (FastAPI serves both the API and the SPA static files)
uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
# Visit http://server:8000
```

### 7.3 Data initialization

```bash
# First deployment: import historical CSV data
uv run python -m scripts.import_data

# Afterwards: upload new data through the web UI "Batch import"
```

## 8. Implementation status and follow-up TODOs

> Superseded in part: this status table reflects v2.1 (May 2026). Many "to be implemented" items below are now done — route-level auth guard, the users/logs APIs, and the AI alignment endpoints all exist. See the inline corrections.

### 8.1 Completed (v2.1)

| Feature | Modules involved | Notes |
|------|---------|------|
| Reasonable historical-low basis (IQR-filtered minimum + source project/date) | `services/history/comparison.py` | Replaces the old mean-price basis |
| Three-level deviation color flag (normal/yellow/red, thresholds configurable per category) | `services/history/comparison.py`, `routes/quotes.py` | Deprecates green/blue/corrected-Z |
| Horizontal comparison-matrix API | `services/matrix/bid_matrix.py`, `routes/analysis.py` | `POST /api/analysis/bid-matrix` |
| Brand-tier model + CRUD + import-popup trigger | `models/brand_tier.py`, `routes/brand_tiers.py` | Import returns `unknown_brands` |
| Material dedup (category + standard_name + spec) | `services/ingestion/import_service.py` | Prevents duplicate storage |
| Supplier-column recognition (independent of the brand column) | `services/ingestion/import_service.py` | Fixes the brand-mistaken-as-supplier issue |
| Auto-refresh material baselines after import | `services/ingestion/import_service.py` | Calls `refresh_material_baselines` |
| Supplier-scoring short-key weights (price/history/completeness/brand/commercial) | `services/history/scoring.py`, `core/config.py` | Consistent with the frontend SettingsView *(corrected 2026-06-23: now long keys; brand dimension removed 2026-06-06.)* |
| Scoring brand dimension switched to BrandTier hit rate | `services/history/scoring.py` | Replaces the old static brand score *(corrected 2026-06-23: brand dimension subsequently removed from scoring.)* |
| Dashboard heatmap + bubble endpoints | `services/history/statistics.py`, `routes/analysis.py` | Frontend wired up |
| Config validation (weights sum ≈ 1, threshold yellow < red) | `routes/config.py` | PUT `/api/config/{key}` |
| JWT login endpoint | `routes/auth.py` | Env vars `JWT_SECRET`, `ADMIN_USER`, `ADMIN_PASS` |
| Extended-attribute schema initialization | `routes/config.py` | `_init_defaults` writes automatically |
| `total_price` auto-computed | `routes/quotes.py` | `unit_price × quantity` on quote create |
| CORS read from environment variables | `main.py` | Env var `CORS_ORIGINS` |

### 8.2 To be implemented (later iterations)

| Priority | Feature | Modules involved | Notes |
|--------|------|---------|------|
| P1 | Switchgear-cabinet BOM component-split import | `services/ingestion/import_service.py` | Must parse in-cabinet component rows and generate independent material+quote records |
| P1 | Route-level permission guard | `routes/` middleware | Login endpoint done; per-route token check not yet enabled *(corrected 2026-06-23: done — `main.py` wraps every router except auth in `Depends(get_current_user)`.)* |
| P2 | User-management API | new `routes/users.py` | Frontend `/system/users` page done *(corrected 2026-06-23: `routes/users.py` exists and is registered.)* |
| P2 | Operation-log API | new `routes/logs.py` | Frontend `/system/logs` page done *(corrected 2026-06-23: `routes/logs.py` exists and is registered; `OperationLog` model present.)* |
| P2 | OCR parsing implementation | `routes/quotes.py` (`/ocr`) | Route registered; wire up an OCR engine or third-party API *(corrected 2026-06-23: implemented differently — recognition lives in `apps/api/intelligence/` (two-stage DashScope OCR + LLM) driven via `routes/intake.py`, not `routes/quotes.py`.)* |
| P2 | Bid-invitation-suggestion API | `routes/invite.py` | Frontend page done; recommendation logic to be implemented *(corrected 2026-06-23: `routes/invite.py` exists and is registered.)* |
| P2 | Comparison-report Excel export | `routes/analysis.py` | F6.4; `scripts/export_excel.py` can be referenced *(corrected 2026-06-23: a dedicated `routes/export.py` now exists and is registered.)* |
| P2 | Data flow / TODO queue | new API + frontend | F1.4, list-status management |
| P3 | Material-code integration | `models/material.py` | Pending the first-construction party's own coding scheme |
| P3 | Bubble-chart brand-tier coloring | `services/history/statistics.py` | `BubbleItem.tier` field currently returns null |

### 8.3 Database migration notes (v1 → v2.1)

> Superseded by `13-Alembic迁移引入.md`. The manual `ALTER TABLE` / `create_all` approach below is obsolete. The current scheme (option B) is: `create_all` bootstraps a brand-new DB to the full model schema; the legacy `_ensure_sqlite_schema` path is FROZEN (no new entries); and ALL new schema changes go through versioned Alembic migrations (`apps/api/migrations/versions/`), stamped from the `0001_baseline` anchor on first run. See `apps/api/core/database.py` `init_db()`.

Existing databases need columns added manually:

```sql
-- Add brand_tier column to quotes
ALTER TABLE quotes ADD COLUMN brand_tier VARCHAR(20) DEFAULT '';

-- Add ref_price_reasonable_low column to materials
ALTER TABLE materials ADD COLUMN ref_price_reasonable_low FLOAT;

-- Create the brand_tiers table (if not auto-created)
-- SQLAlchemy create_all handles new tables automatically on app restart
```

Refresh existing material baselines:
```bash
curl -X POST http://localhost:8000/api/analysis/refresh-baselines
```

Optional: install PyJWT to enable real JWT (without pyjwt, it falls back to a static-token mode):
```bash
uv add pyjwt
```

*(corrected 2026-06-23: the PyJWT fallback still exists in `routes/auth.py` — `static-<username>` tokens when `jwt` is unimportable — so this note remains accurate.)*
