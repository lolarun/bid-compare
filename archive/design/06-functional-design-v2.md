# MEMPAS Functional Design v2

> **Status — audited 2026-06-23.** Partially superseded. The endpoints it claims still exist (`POST /api/analysis/bid-matrix`, `/dashboard`, `/dashboard/heatmap`, `/dashboard/bubble` are all present in `apps/api/routes/analysis.py`), but the document predates the anchor/tender-list bid-comparison flow. The TenderAnchor-based alignment, three-tier quality gating, and evaluation-policy model are NOT described here — see the superseded notes below and `docs/design/05-bid-comparison-intelligence-layers.md`, `06-bid-flow-v2.3-rework.md`, `09`, `12`.
> _Originally written May 2026 (v2.2, 2026-05-20). English translation of the Chinese original; now the authoritative version._

> **Superseded scope note (2026-06-23):** This is the original historical-price-driven product spec. The "reasonable historical low" deviation model (F6.1/F6.2) describes the historical-price comparison surface, not the current tender→anchor→submission bid-comparison flow. The current flow's row axis is `TenderAnchor`, column identity is `BidSubmission.id`, alignment lands in `BidAlignmentGroup`, and evaluation/recommendation is governed by quality tiers (AUTO/REVIEW/BLOCKED) and an evaluation-policy model — none of which is described below. Read this document for the historical-data product surface and the May-2026 user-feedback decision record; read `docs/design/05`, `09`, `12` for the bid-comparison flow.

> Mechanical & electrical material query and bid-comparison analysis system — functional module definitions (revised per 2025-05-19 user feedback)

## Revision history

| Version | Date | Change description |
|------|------|---------|
| v1 | 2025-05-18 | Initial functional design |
| v2 | 2025-05-19 | Per first-construction-bureau (一建) feedback: bid-comparison baseline changed to reasonable historical low; removed modified Z-score; added brand tiers; switchgear cabinet compared by component; added dashboard visualizations |
| v2.1 | 2026-05-19 | All backend P0/P1 features implemented; annotated each module's implementation status; switchgear-cabinet BOM component split deferred to next phase |
| v2.2 | 2026-05-20 | Mr. Liang's (梁老师) feedback landed: switchgear-cabinet BOM split cancelled → horizontal bid-comparison only; brand = cabinet manufacturer; water tank / HVAC pump flagged as data-sparse; fan coil being collected by the customer |

---

## 1. System roles

| Role | Description | Permissions |
|------|------|------|
| Administrator | system config, data import, brand-tier management | all features |
| Bid-comparison operator | day-to-day bid-comparison operations | query, bid-comparison, export |
| Viewer | view results | read-only |

> Phase 1 implements only the Administrator role; no permission control.

## 2. Functional module overview

```
MEMPAS
├── F1 Dashboard
│   ├── F1.1 Data summary cards
│   ├── F1.2 Tree heatmap (project → category → amount)
│   ├── F1.3 Bubble chart (category → supplier → amount)
│   └── F1.4 Data flow / to-do queue
├── F2 Material management
│   ├── F2.1 Material list (CRUD + filter + search)
│   ├── F2.2 Material classification tree (profession → category → subcategory)
│   ├── F2.3 Material name standardization
│   └── F2.4 Extended-attribute management (dynamic schema per category)
├── F3 Supplier management
│   ├── F3.1 Supplier list (CRUD)
│   ├── F3.2 Supplier scoring (5-dimension model)
│   └── F3.3 Multi-supplier comparison
├── F4 Project management
│   └── F4.1 Project list (CRUD + status transitions)
├── F5 Quote management
│   ├── F5.1 Quote records (CRUD + deviation labels)
│   ├── F5.2 Batch import (Excel/CSV upload parsing)
│   └── F5.3 Brand-tier management (popup write-in on import)
├── F6 Tender bid-comparison analysis (core)
│   ├── F6.1 Horizontal comparison matrix
│   ├── F6.2 Single-item price comparison (reasonable historical low baseline)
│   ├── F6.3 Category statistical analysis
│   └── F6.4 Bid-comparison report export
├── F7 System settings
│   ├── F7.1 Scoring weight configuration
│   ├── F7.2 Deviation threshold configuration
│   └── F7.3 Brand-tier mapping table
└── F8 Data import/export
    ├── F8.1 Excel batch import
    ├── F8.2 CSV import
    └── F8.3 Analysis-result Excel export
```

## 3. Module details

### F1 Dashboard

#### F1.1 Data summary cards

| Display item | Data source |
|--------|---------|
| Total materials | `materials` count |
| Total suppliers | `suppliers` count |
| Total projects | `projects` count |
| Total quotes | `quotes` count |
| Per-category stats | grouped by profession: material count / quote count / mean price / CV / supplier count |

#### F1.2 Tree heatmap

- **First level**: total purchase amount of all project materials within a selectable time window
- **Second level**: expanding a project block shows the total purchase amount of each subcategory within the project
- **Visual rule**: bigger = redder, smaller = bluer
- Can filter by time window

#### F1.3 Bubble chart

- **First level**: shows all material categories
  - color = profession (electrical / plumbing / HVAC)
  - size = total purchase amount (amount noted in small text)
- **Second level**: suppliers under a category
  - bubble size = supplier total contracted amount
  - color = brand tier (if classified)
  - amount noted in small text

#### F1.4 Data flow / to-do queue

- categorized by project and list, like a processing queue
- list remark status: recognizing, pending review
- read-only display, no action buttons

### F2 Material management

#### F2.1 Material list

- paginated table: code, name, profession, category, subcategory, spec, reasonable historical low, reference mean price, CV
- filters: profession, category, subcategory (cascading)
- search: name / code / spec keyword
- actions: add, edit, delete, view details
- **Code note**: 一建 is writing its own coding system; the system builds the framework first, leaving the number blank, to integrate later

#### F2.2 Material classification tree

- left-side tree: profession → category → subcategory
- clicking a node filters the right-side material list
- each node shows a count

#### F2.3 Material name standardization

| Type | Example | Standard form |
|------|------|---------|
| Spec synonym | DN100 / 100mm / 4寸 / Φ108 | DN100 |
| Name synonym | 热浸镀锌 / 热镀锌 | 热浸镀锌 |
| Category synonym | 电缆桥架 / 线缆桥架 | 桥架 |
| Dimension format | 300*150 / 300×150 | 300×150 |

#### F2.4 Extended-attribute management

- each category has different extended attributes (Layer 2)
- attributes fall into two classes:
  - **[match]**: must be equal to be comparable (e.g. spec, material, DN)
  - **[difference]**: may differ but must be explained (e.g. thickness, surface treatment)
- stored as JSON; the frontend renders the form dynamically per category
- 一建 is confirming the match/difference split of each category item by item

### F3 Supplier management

#### F3.1 Supplier list

- fields: name, short name, contact, phone, business categories, win count, cooperation score
- new-supplier marking (win_count = 0 → risk flag)

#### F3.2 Supplier scoring (5-dimension model)

| Dimension | Weight | Calculation |
|------|------|---------|
| Price competitiveness | 40% | quote deviation rate mapped to 0-100 |
| Cooperation history | 20% | win-count mapping |
| Quote completeness | 15% | valid quotes / required quotes |
| Brand compliance | 15% | brand-tier hit rate |
| Commercial terms | 10% | manually entered 1-100 |

#### F3.3 Multi-supplier comparison

- select 2-5 suppliers
- same category or project scope
- side-by-side display: price level, scoring radar chart, history

### F4 Project management

- status transitions: in progress → completed / paused
- linked to quote records

### F5 Quote management

#### F5.1 Quote records

- fields: material, supplier, project, tax-inclusive unit price, tax-exclusive unit price, tax rate, quantity, brand, brand tier, deviation rate, deviation color flag
- on quote creation, automatically compute the deviation rate (against the reasonable historical low)
- deviation color flag:
  - within 5% → no color (normal)
  - 5%-10% → yellow (attention)
  - \>10% → red (abnormally high)
  - thresholds adjustable in system settings
- **No alarm-value concept**, only deviation value + color flag

#### F5.2 Batch import

- supports Excel (.xlsx/.xls) and CSV upload
- choose the parsing template by category
- preview after parsing → confirm import
- automatically create non-existent materials and suppliers

#### F5.3 Brand-tier management

- **Brand participates uniformly in comparison**, as a match item
- only distinguishes brand tier (e.g. tier 1 / tier 2 / tier 3), not the specific brand
- 一建 provides the brand-tier draft
- on import, if a **first-time-encountered brand** appears, a popup asks the user to write in the tier
- the tier mapping table can be maintained in system settings

### F6 Tender bid-comparison analysis (core)

#### F6.1 Horizontal comparison matrix (core page)

The multi-supplier horizontal bid-comparison view for the current tender project.

**Top summary**:
- total materials, number of bidding suppliers, recommended main supplier, optimal-combination total price, number of anomalies

**Matrix structure**:

| Column | Description |
|----|------|
| Material | material name + spec description |
| Historical mean price | IQR-filtered mean of historical data, with **time range** and **project source** |
| Reasonable historical low | smallest value at or above Q1-1.5IQR (primary baseline), with **time** and **project** |
| Supplier A~N | each supplier's quote unit price, deviation rate (vs reasonable historical low), total price |
| Lowest deviation | the minimum deviation rate across all suppliers |
| Recommendation | recommended-supplier letter mark |

**Cell rules**:
- green background: lowest price for that item
- gray background: no quote
- green "lowest" badge: marks the lowest quote
- deviation-rate color flag: 5-10% yellow, >10% red, thresholds adjustable

**Bottom summary**:
- total price (sum of quoted materials)
- average deviation

**Actions**:
- filter (category / subcategory)
- export matrix

#### F6.2 Single-item price comparison

Input: category, subcategory (optional), new quote amount
Output: reasonable historical low, historical mean price, P10-P90, deviation rate, color flag

**Bid-comparison baseline definitions** (confirmed by user feedback):

| Baseline | Calculation | Use |
|------|---------|------|
| **Reasonable historical low** (primary) | min value after IQR filtering = min(prices where price >= Q1 - 1.5*IQR) | deviation-rate computation |
| Historical mean price | arithmetic mean after IQR filtering | auxiliary reference |
| Lowest historical price | absolute lowest across all historical data | reminder (retained but not used as a baseline) |

**Deviation-rate computation**:
```
deviation_pct = (new_price - reasonable historical low) / reasonable historical low × 100%
```

**Color-flag determination** (no longer using green/yellow/red/blue alarms):
```
|deviation| ≤ 5%   → no color (normal)
5% < |deviation| ≤ 10% → yellow (attention)
|deviation| > 10%  → red (abnormal)
thresholds configurable per category in system settings
```

#### F6.3 Category statistical analysis

- per-subcategory price distribution (count / mean / median / std dev / CV / P10 / P90)
- conditional formatting: CV < 0.5 green, 0.5-1.0 yellow, > 1.0 red
- ~~abnormal-price detection (Modified Z-score > 3.0)~~ → **cancelled**, modified Z-score no longer used

#### F6.4 Bid-comparison report export

Generates a standard Excel report containing the horizontal comparison matrix, category statistics, etc.

### F7 System settings

#### F7.1 Scoring weight configuration
- 5 dimensions sum to 100%, adjustable

#### F7.2 Deviation threshold configuration
- yellow/red thresholds settable per category
- defaults: yellow 5%, red 10%

#### F7.3 Brand-tier mapping table
- brand → tier (tier 1 / tier 2 / tier 3)
- 一建 provides the draft; the system can maintain it
- popup write-in when a new brand is imported

### F8 Data import/export

#### F8.1 Excel batch import

Parsing templates for 10 categories:

| Category | Key columns |
|------|--------|
| 桥架 (cable tray) | name, spec, material, thickness×3, unit price, brand |
| 阀门 (valve) | name, spec, model, material×5, tax-inclusive total, brand |
| 风口风阀 (air outlet / damper) | name, model, spec, steel-plate thickness, tax-inclusive unit price, brand |
| 母线槽 (busway) | name, busbar type, spec/model, copper-bar thickness, tax-inclusive unit price, brand |
| 配电箱 (switchgear cabinet) | cabinet number, configuration size, configured brand (cabinet manufacturer), main incoming-line spec, unit price (horizontal comparison per whole cabinet) |
| 不锈钢管 (stainless steel pipe) | name, spec, wall thickness, grade, tax-inclusive unit price, brand |
| 水箱 (water tank) | name, spec/model, tax-inclusive total, brand |
| 潜水泵 (submersible pump) | name, model, flow/head/power, unit price, brand |
| 风机盘管 (fan coil) | name, model, piping, air volume, total unit price, brand |
| 空调泵 (HVAC pump) | name, spec, flow/head/power, unit price, brand |

#### Switchgear-cabinet special handling (updated 2026-05-20)

- ~~not compared as a whole, but split by component BOM~~ → **cancelled**
- **horizontal bid-comparison only**: multiple suppliers' cabinet quotes in the same inquiry round are compared directly
- historical data is **stored only**, not participating in the vertical baseline (reasonable_low) analysis
- meaning of the brand field: **cabinet manufacturer** brand (not component brands such as Schneider / ABB)
- when applying brand tiers to the switchgear-cabinet category, what is evaluated is the cabinet manufacturer's tier

#### Data-sparse category note (confirmed 2026-05-20)

The following categories have insufficient historical data; their statistical-analysis results are for reference only:

| Category | Data rows | Supplier count | Note |
|------|--------|----------|------|
| 水箱 (water tank) | 137 | several | customer confirms data is scarce, no supplement planned |
| 空调泵 (HVAC pump) | 128 | several | customer confirms data is scarce, no supplement planned |
| 风盘 (fan coil) | 290 | 3 | customer collecting, to be supplemented |

> The reasonable_low baseline reliability for these categories is limited (insufficient sample size); horizontal bid-comparison is unaffected.

## 4. User-feedback change summary

| # | Change item | Old approach | New approach (confirmed by 一建) |
|---|--------|--------|-------------------|
| 1 | Bid-comparison baseline | historical mean/median | **reasonable historical low** (smallest value at or above Q1-1.5IQR) |
| 2 | Alarm mechanism | four-level alarm green/yellow/red/blue | **deviation value + color flag only** (yellow 5-10%, red >10%, thresholds adjustable) |
| 3 | Outlier detection | modified Z-score > 3.0 | **cancelled**, no longer used |
| 4 | Lowest historical price | not displayed separately | **retained** in the comparison table as a reminder, but not used as a baseline |
| 5 | Brand bid-comparison | brand used only for filtering/scoring | brand as a **match item**, distinguished by **tier** (tier 1/2/3), not by specific brand |
| 6 | Brand-tier entry | none | **first-brand popup** write-in of the tier on import |
| 7 | Switchgear-cabinet bid-comparison | per whole cabinet | ~~per-component comparison~~ → **horizontal bid-comparison only, historical data stored only** (Mr. Liang's 2026-05-20 feedback cancelled the BOM split) |
| 8 | Remark information | LLM extracts payment/warranty/technical | payment, warranty, technical addendum **do not participate in comparison**, shown only as remarks in the result |
| 9 | Material code | system auto-coding | build the framework first, **leave the number blank**, pending 一建's own coding |
| 10 | Horizontal comparison matrix | no such page | **new core page**; historical-mean and reasonable-low columns include time/project info |
| 11 | Dashboard visualization | stats cards only | added **tree heatmap**, **bubble chart**, **data flow / to-do** |

## 5. Phase 1 delivery scope and implementation status

> Legend: ✅ implemented · 🔶 partially implemented · ❌ to be implemented

| Module | Phase 1 scope | Status | Note |
|------|---------|------|------|
| F1 Dashboard | summary cards + tree heatmap + bubble chart | ✅ | frontend display complete; backend `/api/analysis/dashboard`, `/dashboard/heatmap`, `/dashboard/bubble` all wired up |
| F1.4 Data flow | to-do queue | ❌ | deferred to next phase |
| F2 Material | CRUD + filter/search + classification + extended attributes | ✅ | includes name standardization, extended-attribute schema |
| F3 Supplier | CRUD + scoring + multi-supplier comparison | ✅ | 5-dim scoring includes brand-tier hit rate |
| F4 Project | CRUD | ✅ | |
| F5 Quote | CRUD + deviation color flag + batch import + brand-tier popup | ✅ | import auto-triggers baseline refresh; unknown brand returned to frontend popup |
| F5 Switchgear-cabinet BOM | ~~component-level split import~~ | ❌→**cancelled** | 2026-05-20 customer feedback: horizontal bid-comparison only, no BOM split |
| F6.1 Horizontal comparison matrix | API + frontend matrix page | ✅ | `POST /api/analysis/bid-matrix` implemented |
| F6.2 Single-item comparison | reasonable-historical-low baseline + deviation color flag | ✅ | response includes reasonable_low/project/date |
| F6.3 Category stats | subcategory distribution + CV metrics | ✅ | |
| F6.4 Bid-comparison report | Excel export | ❌ | deferred to next phase |
| F7 System settings | weights + thresholds + brand-tier table | ✅ | weight validation sum≈1; threshold validation yellow < red |
| F8 Excel/CSV import | 10-category template parsing | ✅ | includes material dedup, supplier recognition, automatic baseline refresh |
| F8 Scanned-file OCR | PDF/JPG OCR ingestion | 🔶 | API route defined (`/quotes/ocr`); backend parsing logic is mock |
| Authentication | JWT login | 🔶 | `POST /api/auth/login` implemented; route-level permission guard not enabled |
| User management | CRUD + status toggle | ❌ | frontend page exists; backend not implemented |
| Operation log | list + export | ❌ | frontend page exists; backend not implemented |

> _(superseded note 2026-06-23: the F6 status above reflects the historical-price product surface as of May 2026. The endpoints listed still exist in `apps/api/routes/analysis.py`, but the route layer has since grown a large anchor/tender-list surface — `/tender-list/preview|match|confirm`, `/anchor-review/*`, `/bid-alignment/*`, `/bid-matrix/save|versions` — that implements the current tender→anchor→submission flow. That flow is governed by `evaluation_policy.py` and `quote_readiness.py` and is not described in this document.)_

## 6. Items pending 一建 confirmation / supplement

| # | Item | Status |
|---|------|------|
| 1 | Material coding system | 一建 writing it, system leaves blank for now |
| 2 | Match/difference split of each category's extended attributes | 一建 comparing item by item |
| 3 | Brand-tier draft | pending 一建 |
| 4 | Switchgear-cabinet supplier data supplement | scanned version to be added later |
| 5 | Water-tank data supplement (target ≥50 rows) | **customer confirms data is scarce, no supplement planned** (2026-05-20) |
| 6 | Fan-coil supplier supplement (currently only 3) | **customer collecting** (2026-05-20) |
| 7 | HVAC-pump historical-data supplement | **customer confirms data is scarce, no supplement planned** (2026-05-20) |
| 8 | Switchgear-cabinet bid-comparison strategy | **confirmed: horizontal bid-comparison only, historical data stored only** (2026-05-20) |
| 9 | Meaning of switchgear-cabinet brand | **confirmed: cabinet manufacturer brand** (2026-05-20) |

> Detailed feedback analysis: see `docs/design/archive/08-用户反馈分析报告.md` (archived)
