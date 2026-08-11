# Invite Brand Recommendation Design

> **Status — 2026-06-26, initial.** Covers the brand recommendation scoring model for the
> invite (邀标) flow. Related: `apps/api/services/supplier/brand_recommend.py`,
> `apps/api/routes/invite.py`.

---

## 1. Purpose

When a user uploads a tender document, the system infers procurement categories and recommends
approved brands from the `brand_tiers` table, enriched with historical price statistics from
matched `quotes` records.

The recommendation list must be **ranked by a meaningful, explainable score** — not by a
hard tier cutoff — so that a well-documented domestic brand can outrank an undocumented
joint-venture brand.

---

## 2. Scoring Model

### 2.1 Composite score formula

```
score = W_tier × tier_factor + W_data × data_factor

tier_factor  : 合资 (joint venture) = 1.0,  国产 (domestic) = 0.0
data_factor  : log(sample_count + 1) / log(MAX_SAMPLES + 1)
               clamped to [0, 1]

W_tier  = 0.30   # joint-venture soft preference
W_data  = 0.70   # data-confidence majority weight
MAX_SAMPLES = 50  # normalisation ceiling (sample_count above this → data_factor ≈ 1)
```

### 2.2 Rationale for the weights

| Weight | Value | Reasoning |
|--------|-------|-----------|
| `W_tier` | 0.30 | Joint-venture brands carry implied quality assurance in M&E procurement, warranting a soft preference — but not a hard override over data-backed domestic brands. |
| `W_data` | 0.70 | Price reliability increases with sample volume; historical data is the primary differentiator when brands share the same tier. |

### 2.3 Example rankings

| Brand | Tier | Samples | tier_factor | data_factor | score |
|-------|------|---------|-------------|-------------|-------|
| KITZ | 合资 | 20 | 1.0 | log(21)/log(51) ≈ 0.77 | 0.30 + 0.54 = **0.84** |
| WATTS | 合资 | 5 | 1.0 | log(6)/log(51) ≈ 0.46 | 0.30 + 0.32 = **0.62** |
| 上海良工 | 国产 | 15 | 0.0 | log(16)/log(51) ≈ 0.68 | 0.00 + 0.48 = **0.48** |
| 班尼戈 | 合资 | 0 | 1.0 | 0.00 | 0.30 + 0.00 = **0.30** |
| 上海冠龙 | 国产 | 0 | 0.0 | 0.00 | 0.00 + 0.00 = **0.00** |

A zero-sample domestic brand always scores 0; a zero-sample joint-venture brand scores 0.30
(the bare tier bonus). A domestic brand with ≥ 7 samples (~`data_factor` > 0.54) can overtake
a zero-sample joint-venture brand.

### 2.4 Constants location

All constants (`W_TIER`, `W_DATA`, `MAX_SAMPLES`) are defined at module top-level in
`apps/api/services/supplier/brand_recommend.py` and must not be scattered elsewhere.

---

## 3. Category inference

Categories are inferred from tender item names via `infer_categories()` in
`apps/api/services/supplier/supplier_recommend.py`:

1. **Token-boundary match** — item name contains the exact category string (e.g., "桥架").
2. **Keyword alias map (`CATEGORY_KEYWORD_MAP`)** — fallback for sub-types not containing the
   parent string (e.g., "截止阀" → "阀门", "电缆桥架" → "桥架").
3. **`category` field pass-through** — frontend passes `category` from the recognition result;
   the backend uses it directly when present, avoiding re-inference.

---

## 4. Data contract

- Only `BrandTier` rows with `is_approved = True` are considered candidates.
- Price statistics are computed over `Quote` records passing `valid_quote_filters()` (see
  `docs/design/11-historical-price-governance.md`). Quotes with `supplier_id = None` pass
  unconditionally.
- `sample_count` is the count of matched historical quote rows, not the total brand occurrence.
- If `sample_count = 0`, price fields (`price_p10`, `price_median`, `price_p90`) are `null`.

---

## 5. Out of scope

- **LLM re-ranking**: The LLM may only explain deterministic results; it must not re-rank
  candidates (per `CLAUDE.md §4`). Brand scoring is fully deterministic.
- **Per-item brand matching**: The current model recommends brands per inferred category, not
  per individual tender line item. Per-item matching is a future enhancement.
- **Supplier-level evaluation**: Brand recommendation is independent of the bid-comparison
  scoring model in `docs/design/02-comparison-weight-design.md`.
