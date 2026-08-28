# Comparison Weight Design

> **Status — audited 2026-06-23.** Partially superseded. The 5-dimension supplier-scoring model in this doc has been reduced to **4 dimensions in `apps/api/services/history/scoring.py`**: the `品牌/标准合规` (brand/standard compliance) dimension was removed on 2026-06-06 — manual bid comparison never scores by brand tier — and its 15% was redistributed (price 40%→45%, history 20%→25%, completeness/commercial unchanged at 15%). Known structural gap: Layer-A weighted comparison assumes structured per-category attributes, but in practice only `brand` is reliably extracted; the implemented scoring therefore relies on price/history/completeness/commercial signals, not on the extended structured attributes this doc implies.
> _Originally written (date not recorded in source). English translation of the Chinese original; now the authoritative version._

> Reference: the SAP supplier-evaluation system (ME61), adapted to the tender bid-comparison scenario of the building M&E installation industry.

## 1. Two-Layer Weight System

Comparison weights are split into two layers, each solving a different problem:

- **Layer A: supplier composite score** — answers "which supplier to choose"
- **Layer B: per-item price deviation tolerance** — answers "is this quote anomalous"

## 2. Layer A: Supplier Composite Score Weights

### 2.1 Scoring Dimensions and Weights

| Dimension | Weight | Scoring mode | Data source | Notes |
|------|------|---------|---------|------|
| **Price competitiveness** | 40% | Automatic | This quote vs historical baseline price | Core metric; the closer to / below baseline, the higher the score |
| **History cooperation** | 20% | Automatic | Historical win count, cooperation frequency | Reflects supplier reliability |
| **Quote completeness** | 15% | Automatic | Items required / items actually quoted | Many missing items indicate weak supply capability |
| **Brand / standard compliance** | 15% | Semi-automatic | Whether the execution standard meets requirements | Whether it satisfies tech-spec requirements |
| **Commercial terms** | 10% | Manual | Payment terms, lead time, warranty conditions | No prepayment / short lead time earns points |

> (corrected 2026-06-23: the implemented weights in `apps/api/core/config.py` `DEFAULT_SCORING_WEIGHTS` are `price_competitiveness: 0.45`, `history_cooperation: 0.25`, `quote_completeness: 0.15`, `commercial_terms: 0.15`. The **Brand / standard compliance** dimension was dropped entirely on 2026-06-06 and its 0.15 redistributed to price and history. The "Commercial terms" weight is therefore 15%, not 10%, and it is sourced from `Supplier.cooperation_score` rather than a per-bid manual 1–100 entry.)

### 2.2 Per-Dimension Scoring Rules

**Price competitiveness (40%):**

```
score = f(this quote's total price, historical baseline total price)

deviation rate = (this quote − historical baseline) / historical baseline × 100%

deviation rate ≤ -10%    → 100 points (significantly below baseline)
-10% < deviation ≤ 0%   → 80 ~ 100 points (linear interpolation)
0% < deviation ≤ 10%    → 60 ~ 80 points (linear interpolation)
10% < deviation ≤ 20%   → 40 ~ 60 points (linear interpolation)
deviation > 20%     → below 40 points, triggers an alert
```

> (corrected 2026-06-23: `scoring.py` implements this banding per-item against `Material.ref_price_reasonable_low` (the IQR-filtered "reasonable historical low"), falling back to `ref_price_median` — **not** against a quote total vs a historical baseline total. It also floors the worst case at `max(20.0, 40.0 - (avg_dev - 0.20) * 100)` rather than leaving it open-ended.)

**History cooperation (20%):**

```
historical win count ≥ 5   → 100 points
3 ~ 4 times              → 80 points
1 ~ 2 times              → 60 points
0 times (new supplier)    → 40 points + new-supplier risk flag
```

> (corrected 2026-06-23: matches `scoring.py` thresholds, but the implemented code does not attach a "new-supplier risk flag" — it only assigns 40 points for a zero win count.)

**Quote completeness (15%):**

```
completeness = items actually quoted / items required to quote × 100%

100%    → 100 points
80%~99% → proportional score
< 80%   → below 60 points + flag "quote incomplete"
```

> (corrected 2026-06-23: `scoring.py` computes completeness as `valid_quotes / total_quotes × 100` (quotes with `unit_price > 0` over all of the supplier's quotes passing `valid_quote_filters()`), capped at 100, defaulting to 50 when the supplier has no quotes. There is no separate "below 80% → <60 points" cliff or "quote incomplete" flag in the implementation.)

**Brand / standard compliance (15%):**

```
all item brands within the recommended list + standard-compliant  → 100 points
some item brands non-recommended but standard-compliant           → 70 points
standard non-compliant                                            → 40 points + alert
```

> (corrected 2026-06-23: this dimension is **not implemented** — it was removed on 2026-06-06. See the status banner.)

**Commercial terms (10%):**

```
Manual 1–100 score; reference factors:
- Payment terms (no prepayment earns points; required prepayment loses points)
- Lead time (shorter is better)
- Warranty conditions (longer warranty is better)
- After-sales response (a local service point earns points)
```

> (corrected 2026-06-23: the implementation uses `Supplier.cooperation_score` (defaulting to 60 when not positive) rather than a per-bid manual 1–100 entry, and the effective weight is 15%, not 10%.)

### 2.3 SAP Standard Weight Comparison

| SAP standard dimension | SAP weight key 02 | This system's dimension | This system's weight | Difference notes |
|-------------|-------------|--------------|-----------|---------|
| Price | 33.3% | Price competitiveness | 40% | Construction procurement weights price more heavily |
| Quality | 33.3% | Brand / standard compliance | 15% | Assessed indirectly via brand and execution standard |
| Delivery | 13.3% | Commercial terms (partial) | 10% | Merged into commercial terms |
| General service | 6.7% | History cooperation | 20% | Replaces subjective scoring with historical data |
| External service | 13.3% | Quote completeness | 15% | Replaced with a more quantifiable metric |

> (corrected 2026-06-23: this comparison reflects the original 5-dimension model. The current 4-dimension weights are price 45% / history 25% / completeness 15% / commercial 15%; the "Quality / Brand compliance" row no longer exists.)

## 3. Layer B: Per-Item Price Deviation Tolerance

### 3.1 Determining Tolerance from Historical-Data Statistics

Different categories have different price-volatility characteristics; tolerance should be derived statistically from historical data rather than set arbitrarily by hand.

**Computation method:**

```
1. Collect all historical quotes for a category
2. Compute coefficient of variation CV = standard deviation / mean
3. Tolerance threshold = max(CV × adjustment factor, minimum tolerance)
   - Suggested adjustment factor: 1.5 ~ 2.0
   - Suggested minimum tolerance: 5%
```

### 3.2 Suggested Per-Category Tolerances (pending data validation)

The following are initial suggested values; they must be calibrated statistically once the customer's complete historical data is available:

| Category | Suggested deviation tolerance | Alert level | Rationale |
|------|------------|---------|------|
| Cable tray (standard part) | ±10% | Yellow alert beyond; red beyond 20% | Mature market, high spec standardization |
| Valve | ±15% | Yellow alert beyond; red beyond 25% | Brand differences cause larger price spreads |
| Pipe (steel pipe, PPR) | ±12% | Yellow alert beyond; red beyond 20% | Affected by steel / raw-material market |
| Distribution box | ±20% | Yellow alert beyond; red beyond 30% | Much non-standard customization, large spec variance |
| Diffusers & dampers | ±15% | Yellow alert beyond; red beyond 25% | A pricing formula is available for reference |
| Cable | ±8% | Yellow alert beyond; red beyond 15% | Copper price transparent; spread mainly from brand |
| Special equipment (photocatalyst, etc.) | ±25% | Yellow alert beyond; red beyond 35% | Opaque market, few suppliers |

> (Unverifiable in this pass: the exact per-category default tolerance values live in `DEFAULT_THRESHOLDS` in `apps/api/core/config.py` as `{yellow, red}` pairs — not audited line-by-line here. Treat the numbers above as the design-time suggestion, not a guaranteed reflection of the current config.)

### 3.3 Alert Display Rules

```
deviation ≤ tolerance         → green (normal)
tolerance < deviation ≤ 2×tolerance → yellow alert (needs attention)
deviation > 2×tolerance        → red alert (anomalous, verification recommended)
deviation < -tolerance         → blue notice (below expectation; possible spec/quality difference)
```

### 3.4 Choosing the Comparison Baseline

| Baseline type | Computation | Applicable scenario |
|---------|---------|---------|
| Historical average | Arithmetic mean of all historical records | Default baseline |
| Historical median | Median of all historical records | More robust when extreme values exist |
| Recent average | Mean of records from the last 6 months | When tracking recent market conditions |
| Lowest historical price | Lowest historical transaction price | Negotiation floor reference |
| Last-round awarded price | Last awarded price for the same category | Most direct benchmark |

The system uses "recent average" as the comparison baseline by default; the user may switch.

## 4. Weight Configurability

All weights and tolerances should support adjustment by an administrator within the system:

- The 5 supplier-scoring dimension weights are adjustable (sum = 100%)
- Per-category deviation tolerance can be set individually
- The alert-level multiplier thresholds are adjustable
- The comparison-baseline type can be switched

Initial values are generated by system presets plus historical-data statistics, then fine-tuned by an administrator based on business experience.

> (corrected 2026-06-23: weights are configurable via the `scoring_weights` `AnalysisConfig` row and read by `get_scoring_weights()`; thresholds via the `thresholds` config row. However, there are now **4** scoring dimensions, not 5 — see §2.1.)
