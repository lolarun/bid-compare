"""Centralised domain safety thresholds — single source of truth.

Tier classification (CLAUDE.md §7):
  SYSTEM  — resource limits: env-driven (OCR_RENDER_SCALE, PAGE_CONCURRENCY …)
  DOMAIN  — quality-gate thresholds defined here; change requires code review
  PROJECT — per-project evaluation rules stored in EvaluationPolicy / DB

All MATCH_* constants below are DOMAIN tier.
"""

# ── Match / quality-gate thresholds ─────────────────────────────────────────

# Min fraction of eligible rows that must have unit_price > 0
MATCH_PRICE_COVERAGE_THRESHOLD: float = 0.80

# Max fraction of evaluable rows allowed to have a hard arithmetic error
MATCH_ARITHMETIC_MAX_ERROR_RATE: float = 0.05

# VAT deviation tolerance: 11.5% (≈13%/113%) + 1% rounding allowance
MATCH_ARITHMETIC_VAT_TOLERANCE: float = 0.125

# Systematic VAT mismatch gate: block when >20% of rows deviate at ~11–12.5%
MATCH_VAT_MISMATCH_BLOCK_RATE: float = 0.20

# Single-row concentration gate: one row must not exceed 60% of total
MATCH_MAX_LINE_CONCENTRATION: float = 0.60

# Declared-total tolerance: 3% before raising a completeness flag
MATCH_DECLARED_TOTAL_TOLERANCE: float = 0.03
