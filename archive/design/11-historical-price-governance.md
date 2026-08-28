# Historical Procurement Price Governance and Business Services

> **Status — audited 2026-06-23.** Partially implemented. The governance principles and the `valid_quote_filters()` minimal filter are in place, but the named domain-service façade (`HistoricalPriceService` / `SupplierEvidenceService` / `SupplierBrandEvidenceService` / `InviteRecommendationService`) and the graded supplier-brand-evidence model do not yet exist as code.
> _Originally written as a design baseline (no explicit date in the original). English translation of the Chinese original; now the authoritative version._

> Status: design baseline
> Scope: `docs/项目资料/材料汇总`, customer supplementary versions, formally archived historical procurement prices, and the consumption of historical data by bid-comparison, invitation-to-bid, and supplier recommendation.
> Core principle: historical procurement prices are not a set of `Quote` records that can be queried at will, but a set of business facts that have passed source, basis, entity, and quality review.

## 1. Goals

This process solves two problems at once:

1. Turn the historical Excel files the customer provides into a stable, traceable, re-reviewable data asset.
2. Provide a unified business service for bid-comparison, invitation-to-bid, supplier matching, and agency-brand matching — rather than letting each module query the raw tables directly or assemble its own rules.

The end result is three kinds of consumable facts:

- **Historical price fact**: the confirmed price of a given material from a given supplier on a given project, at a given date and tax-price basis.
- **Supplier performance fact**: in which categories, projects, and time ranges a supplier has a credible supply record.
- **Supplier-brand evidence**: evidence that a supplier has quoted, supplied, or been explicitly registered as agent for a given brand; different evidence levels must never be conflated.

## 2. Non-goals and safety boundaries

- Raw Excel, raw CSV, and cleaning-intermediate files must not enter the production price baseline directly.
- Bid-comparison staging data `BidSubmission` / `BidQuoteLine` does not automatically become a historical procurement price.
- E2E, test projects, demo data, and ad-hoc repair data must not enter the historical baseline, supplier history, or brand evidence.
- Supplier names and brand names in raw files must not automatically create or merge supplier master data.
- "A supplier once quoted a brand" is not equal to "the supplier holds a formal agency authorization for that brand."
- Historical data may only assist invitation-to-bid and evaluation; it does not replace supplier admission, agency-authorization verification, or procurement approval.

## 3. Data catalog and versions

The CSV files and `manifest.json` in the existing `docs/data` root are treated as legacy v0, frozen and preserved, not overwritten by regeneration.

New data uses explicit version directories:

```text
docs/data/
├─ raw/
│  └─ YYYY-MM-DD/
│     ├─ manifest.json
│     ├─ source_registry.json
│     ├─ csv/
│     │  ├─ 不锈钢管/
│     │  ├─ 桥架/
│     │  ├─ 母线槽/
│     │  ├─ 配电箱/
│     │  ├─ 阀门/
│     │  └─ ...
│     └─ audits/
│        ├─ workbook_summary.json
│        ├─ source_version_diff.csv
│        └─ conversion_report.json
└─ curated/
   └─ v1/
      ├─ manifest.json
      ├─ categories/
      ├─ mappings/
      ├─ review/
      ├─ rejected/
      └─ reports/
```

Rules:

- The raw directory does only repeatable format conversion; it does not modify business meaning.
- The curated directory holds candidate facts after cleaning, classification, and validation.
- No version may be overwritten in place; a new source or a new rule must produce a new version and a diff report.
- `manifest.json` records file hashes, source versions, conversion parameters, row/column counts, formula counts, CSV hashes, and generation time.

## 4. Source registration

`source_registry.json` is the single source-selection manifest. Each source registers at least:

```json
{
  "source_id": "electrical-panel-2026-05-20",
  "category": "配电箱",
  "path": "docs/项目资料/用户反馈/2026-05-20/0配电箱.xlsx",
  "sha256": "...",
  "version_date": "2026-05-20",
  "status": "active",
  "supersedes": "electrical-panel-original",
  "authority": "customer_feedback",
  "notes": ""
}
```

Source statuses:

- `active`: the authoritative version currently used for cleaning.
- `superseded`: replaced by a newer version, but must be retained.
- `reference_only`: for comparison only; may not generate price facts.
- `rejected`: the source is wrong or unusable.

### 4.1 Electrical-panel version policy

Two versions of the electrical panel (配电箱) currently exist:

- Original: `docs/项目资料/材料汇总/0配电箱.xlsx`
- Customer supplementary version: `docs/项目资料/用户反馈/2026-05-20/0配电箱.xlsx`

Handling rules:

1. The customer supplementary version is the active source; the original is marked superseded.
2. Compute SHA256 for both versions and generate a sheet-, cell-, and formula-level diff report.
3. Brand values added in the new version enter `brand_raw`.
4. Non-brand values in the original's brand column — such as "新增" (newly added), "利用原有箱体" (reuse existing enclosure) — must not be discarded; migrate them to `box_status_raw` or `source_note`.
5. Formula cells record both the formula and the cached display value at the raw layer; curated price facts may use only the verified display value.

## 5. Cleaning pipeline

```text
source registration
  -> raw workbook inventory
  -> raw CSV + manifest
  -> multi-version diff audit
  -> sheet/row-type recognition
  -> field standardization
  -> material, supplier, brand entity resolution
  -> price and tax-basis validation
  -> historical-fact qualification ruling
  -> curated / review / rejected routing
  -> conservation and quality report
  -> human-approved release
```

### 5.1 Raw conversion

- Each workbook/sheet is exported independently; no self-directed merging.
- The `汇总` (summary) sheet and the project detail sheets must annotate their data relationship; aggregating them simultaneously is forbidden until mutual exclusivity is proven, to avoid double counting.
- CSV preserves the original column names, original values, and source coordinates.
- The conversion script must be driven by `source_registry.json`; hard-coding the old directory is not allowed.

### 5.2 Row-type recognition

Each row is classified into at least:

- `price_line`
- `component_line`
- `box_header`
- `subtotal`
- `grand_total`
- `fee`
- `formula`
- `remark`
- `empty`
- `invalid`

Subtotals, grand totals, formula notes, and section headers must not enter the historical unit-price sample.

### 5.3 Field standardization

Keep both raw and normalized:

- Name, specification, model, material, unit
- Quantity, tax-inclusive / tax-exclusive unit price, line total, tax rate
- Supplier, brand, project, date
- Category-specific extension fields, e.g. a valve's DN/PN/valve-type, an electrical panel's enclosure number / circuit / component

Using `(name, spec)` as a global unique key is forbidden. The same material may legitimately recur under different systems, floors, projects, and suppliers.

### 5.4 Electrical-panel-specific model

An electrical panel is a hierarchical BOM and cannot be forced into a flat material price table:

- `配电箱_箱级.csv`: project, enclosure number, enclosure type, quantity, whole-enclosure price, brand, status.
- `配电箱_元件级.csv`: enclosure number, component, spec, model, brand, quantity, unit price, line total.
- `配电箱_费用项.csv`: enclosure body, labor, management fee, auxiliary materials, subtotal, etc.

Only component rows that can be proven to be a separately procured price may enter the component historical-price sample. Whole-enclosure prices must not be mixed with component unit prices.

## 6. Historical-fact qualification

A record may be published as a `historical_price_fact` only if it meets all of the following:

- The source is active and the hash is traceable.
- The row type is a valid price line.
- Material standardization is unambiguous, with the original name and spec retained.
- The price is greater than zero and the currency is clear.
- The tax-price basis is clear; an unknown basis must not be mixed into tax-inclusive / tax-exclusive computations.
- The unit and price-unit are clear.
- Supplier resolution yields a confirmed entity, or is explicitly marked as an unknown supplier.
- It does not belong to a test, E2E, demo, ad-hoc-repair, or contaminated batch.
- There is no unexplained arithmetic conflict, total contamination, or duplicate source.
- It has `source_file/source_sheet/source_row`, and cell coordinates where necessary.

Qualification statuses:

- `approved`: usable by price, supplier, and brand business services.
- `price_only`: the price is credible, but supplier or brand evidence is insufficient.
- `supplier_only`: supplier history can be proven, but the price basis is unsuitable as a baseline.
- `review`: awaiting human verification.
- `rejected`: business consumption forbidden.

## 7. Standard fact contracts

### 7.1 Historical price fact

```text
fact_id
material_id / canonical_material_key
raw_name / standard_name
raw_spec / normalized_spec
category / family / DN / PN / unit
project_id / project_name
supplier_id / supplier_raw_name
brand_id / brand_raw
quantity
unit_price_incl_tax / unit_price_excl_tax
tax_rate / tax_basis
quote_date
fact_status
source_id / source_file / source_sheet / source_row
quality_flags
```

The price baseline must be computed over similar materials, specs, units, tax bases, and time ranges. When the sample is insufficient it returns no baseline; it must not degrade to the all-category minimum price.

> (corrected 2026-06-23: the `historical_price_fact` contract above is not yet a persisted model. The same-spec baseline behavior it requires IS implemented today in `apps/api/services/history/comparison.py`, which keys deviation off `(valve_family, DN, PN, unit, tax_basis)` and returns no baseline when samples are insufficient — see `spec_baseline_from_index`. The general baseline path uses IQR-filtered "reasonable low" from the `Material.ref_price_*` fields refreshed by `apps/api/services/history/statistics.py::refresh_material_baselines`.)

### 7.2 Supplier performance evidence

```text
supplier_id
category / material_family
project_id
first_seen_at / last_seen_at
approved_fact_count
project_count
recent_fact_count
price_sample_count
source_ids
```

Quote count, award count, and win count must be kept separate. Without evidence of a win, "has quoted" must not be described as "has won/transacted."

### 7.3 Supplier-brand evidence

Evidence levels:

1. `authorized`: there is an authorization letter, agency certificate, or customer-confirmed material.
2. `awarded`: a win or procurement fact explicitly records this supplier and brand.
3. `quoted`: a historical quote record shows this supplier quoted this brand.
4. `claimed`: self-stated by the supplier or in the bid document, but not independently verified.
5. `inferred`: inferred by the system from text or context; usable only as a candidate hint.

```text
supplier_id
brand_id / brand_raw
category
evidence_level
project_id
valid_from / valid_to
source_id / source_ref
status
```

The invitation-to-bid page must display the evidence level. `quoted`, `claimed`, and `inferred` must not be presented as "formal agency brand."

> (corrected 2026-06-23: this graded supplier-brand-evidence model is not implemented. No `evidence_level` column or `SupplierBrandEvidence` table exists in `apps/api/models/`. Today, brand evidence is derived ad hoc from `Quote.brand` — exactly the `quoted`-level signal — and `apps/api/services/supplier/supplier_recommend.py::_get_supplier_brands` returns the distinct `Quote.brand` values a supplier has quoted, with no level distinction. This is the gap §9 warns against, still open.)

## 8. Business-service boundaries

Historical procurement prices need to expose domain services, not just CRUD.

> (corrected 2026-06-23: none of the four services named below — `HistoricalPriceService`, `SupplierEvidenceService`, `SupplierBrandEvidenceService`, `InviteRecommendationService` — exist as code. The names appear only in design docs 11 and 12. Today's closest equivalents are `apps/api/services/history/comparison.py` (price baselines), `apps/api/services/supplier/supplier_recommend.py` (invitee recommendation), and `apps/api/services/history/statistics.py` (dashboard/category aggregates). These query `Quote` directly through the shared `valid_quote_filters()` rather than through the façade described here.)

### 8.1 HistoricalPriceService

Responsibilities:

- Retrieve credible samples by standard material key, spec, unit, tax basis, and date.
- Compute median, percentile band, sample size, and data freshness.
- Return the baseline's source projects, supplier coverage, and traceable evidence.
- Explain why there is no reliable baseline.

Suggested interface:

```text
get_price_samples(material_signature, filters)
get_price_baseline(material_signature, tax_basis, as_of)
explain_price_baseline(baseline_id)
get_price_trend(material_signature, period)
```

### 8.2 SupplierEvidenceService

Responsibilities:

- Query a supplier's credible history under a given category, material family, and project type.
- Distinguish quote, procurement, win, and cooperation evidence.
- Output sample size, most-recent time, project coverage, and evidence source.

Suggested interface:

```text
get_supplier_history(supplier_id, scope)
find_suppliers_for_materials(material_signatures, scope)
get_supplier_coverage(supplier_id, material_signatures)
```

### 8.3 SupplierBrandEvidenceService

Responsibilities:

- Query the evidence between a supplier and a brand, rather than querying a source-less string list.
- Support bidirectional retrieval: "which brand evidence does a supplier have" and "which supplier evidence does a brand have."
- Filter by evidence level, validity period, category, and source.

Suggested interface:

```text
get_brands_for_supplier(supplier_id, category, min_evidence_level)
find_suppliers_for_brands(brand_requirements, category)
explain_supplier_brand_evidence(supplier_id, brand_id)
```

### 8.4 InviteRecommendationService

Invitation-to-bid recommendation composes the services above:

1. Generate material signatures and brand requirements from the tender list.
2. Recall candidate suppliers using supplier history.
3. Verify brand coverage using supplier-brand evidence.
4. Provide historical-competitiveness reference using price facts.
5. Return evidence and gaps; do not auto-create suppliers and do not auto-claim agency authorization.

The recommendation result must explain:

- Why each candidate supplier was recalled.
- Which materials and brands it covers.
- How many approved historical facts were used.
- Which brands are backed only by historical-quote evidence.
- Which materials have no historical supplier coverage.

## 9. Unified consumption rules

- Bid-comparison, the dashboard, supplier scoring, and invitation-to-bid must not each implement historical-validity filtering on their own.
- Business code must not treat a `Quote.brand` aggregation result directly as an agency brand.
- All historical business queries must go through the unified service, and by default consume only approved / permitted-use facts.
- The current `valid_quote_filters()` is only a minimal database filter; it does not constitute full archival qualification.
- After any historical fact is re-cleaned or re-released, the price baseline and supplier/brand evidence must be rebuildable and reconcilable.
- When a service returns an empty result, the caller must show "insufficient evidence" and must not fall back to an unscoped whole-database statistic.

> (corrected 2026-06-23: `valid_quote_filters()` exists at `apps/api/services/history/quote_filters.py` and IS the shared minimal filter — it excludes non-`active` suppliers (`Supplier.merge_status != 'active'`) and quotes flagged `polluted` / `excluded_from_ref` (`Quote.bid_status`). It is correctly reused by `statistics.py`, `supplier_recommend.py`, and the baseline refresh. The remaining unmet rules: (a) `supplier_recommend.py::_get_supplier_brands` treats `Quote.brand` aggregation as brand signal, contrary to bullet 2; (b) there is no unified service façade, so callers query `Quote` directly; (c) the dashboard heatmap/bubble queries in `statistics.py` do NOT apply `valid_quote_filters()` — they filter only on `Quote.bid_status != "未中标"`, an inconsistency with bullet 1.)

## 10. Release and database writes

Curated files may be released to the business database only after acceptance:

1. Back up the production database.
2. Dry-run: output the list of added, updated, rejected, pending-review, and duplicate rows.
3. Each written record carries `source_id`, the data version, and the qualification status.
4. Write inside a transaction; roll back if any conservation assertion fails.
5. Do not physically delete old facts; use statuses such as `superseded`, `polluted`, `excluded_from_ref`.
6. After release, rebuild the price baseline and evidence index, and reconcile sample counts and source counts.

Archiving a bid-comparison quote must be an explicit user action:

```text
BidSubmission / BidQuoteLine
  -> user confirms archival
  -> archival quality gate
  -> Quote / historical fact candidate
  -> review and release
```

## 11. Acceptance gates

### Raw layer

- The source file count, sheet count, and row/column counts are conserved against the workbook.
- Formula count, cached values, and file hashes are recorded.
- Multi-version sources have an explicit active/superseded relationship.

### Curated layer

- Every record has source coordinates.
- Subtotal, grand-total, formula, and note rows have not contaminated the price sample.
- Tax-inclusive / tax-exclusive bases are not mixed in computation.
- Duplicate and rejected records are reported.
- Electrical-panel enclosure-level, component-level, and fee items are conserved.

### Service layer

- Test / E2E / contaminated data does not affect the price baseline or supplier recommendation.
- When there is no reliable same-spec sample, an empty baseline is returned.
- Supplier-brand results include evidence level and source.
- Invitation-to-bid recommendation can explain supplier recall, brand coverage, and data gaps.
- Bid-comparison staging does not automatically contaminate historical prices or supplier master data.

## 12. Phased implementation

1. **Raw asset rebuild**: fix conversion-script paths, establish the source registry, and generate versioned raw CSV and manifest.
2. **Cleaning and audit**: build a curator per category; prioritize valves and electrical panels; output review/rejected/conservation reports.
3. **Unified historical-service façade**: first implement the service layer on top of the existing `Quote` table, replacing each module's direct aggregation queries.
4. **Supplier-brand evidence model**: establish evidence levels, sources, and validity periods; migrate existing quote brands to `quoted` evidence.
5. **Invitation-to-bid integration**: supplier recommendation consumes the historical service, displays evidence, and does not pass off quote brands as agency authorization.
6. **Formal release**: after approval, rebuild production historical prices and verify bid-comparison, invitation-to-bid, supplier profiles, and the dashboard.

> (corrected 2026-06-23: of the six phases above, only the precursors are partially done. Phase 3's "service façade" is not built — callers still query `Quote` directly. Phase 4's evidence model is not built. Phases 1, 2, 5, and 6 remain design intent.)
