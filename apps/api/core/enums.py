"""Domain-level string constants — canonical vocabulary for three status axes (§10.4).

Three axes that must NOT be conflated:
  Cell status  — what a cell in the bid matrix contains (CELL_*)
  Quality gate — extraction/confirmation grade (QG_*)
  Recommendation — final recommendation level (REC_*)
  Row type     — structural type of a quote/tender line (RT_*)

All string values are intentionally lowercase except quality-gate codes which
are uppercase (AUTO / REVIEW / BLOCKED) to match the extraction_draft convention.
Never compare across axes; they share words like "blocked" but mean different things.
"""

# ── Cell status (bid matrix) ──────────────────────────────────────────────────
CELL_QUOTED = "quoted"          # confirmed align item with price
CELL_AGGREGATED = "aggregated"  # multi-row aggregated align item
CELL_PENDING = "pending"        # pending — show price, exclude from eval totals
CELL_EXCLUDED = "excluded"      # explicitly excluded by user
CELL_MISSING = "missing"        # supplier did not quote this anchor row

# ── Quality gate (extraction grade) ──────────────────────────────────────────
QG_AUTO = "AUTO"
QG_REVIEW = "REVIEW"
QG_BLOCKED = "BLOCKED"

# ── Recommendation level (bid_matrix._compute_recommendation) ─────────────────
REC_BLOCKED = "blocked"
REC_CONDITIONAL = "conditional"

# ── Row type — canonical vocabulary (§11.4) ───────────────────────────────────
# All production code (table_parser, table_recognizer, BidQuoteLine) must use
# these values.  Old names (header / note / empty) are normalised on ingress.
RT_QUOTE_LINE = "quote_line"
RT_SECTION_HEADER = "section_header"
RT_REMARK = "remark"
RT_INVALID = "invalid"
RT_SUBTOTAL = "subtotal"
RT_GRAND_TOTAL = "grand_total"

# Row types that must not be included in quote-line counts or eval totals
RT_NON_QUOTE = frozenset({RT_SECTION_HEADER, RT_REMARK, RT_INVALID,
                           RT_SUBTOTAL, RT_GRAND_TOTAL})

# ── User roles ───────────────────────────────────────────────────────────────
ROLE_ADMIN = "管理员"
ROLE_BUYER = "比价员"
ROLE_VIEWER = "查看者"
ALL_ROLES = frozenset({ROLE_ADMIN, ROLE_BUYER, ROLE_VIEWER})
