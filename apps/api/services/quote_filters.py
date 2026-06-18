"""Shared "valid historical quote" filter — single source of truth.

All reference-price queries MUST call valid_quote_filters() to exclude:
  - Quotes from merged/inactive suppliers (Supplier.merge_status != 'active')
  - Quotes explicitly flagged as polluted or excluded from reference prices
    (Quote.bid_status IN ('polluted', 'excluded_from_ref'))

Usage (caller must join Supplier before applying):
    q = db.query(Quote).join(Supplier, Quote.supplier_id == Supplier.id)
    q = q.filter(*valid_quote_filters())

    # Or use the convenience function:
    q = valid_quote_query(db)
"""

from sqlalchemy.orm import Session

from apps.api.models.quote import Quote
from apps.api.models.supplier import Supplier

# bid_status values that must NEVER contribute to reference-price statistics
_EXCLUDED_BID_STATUSES = frozenset({"polluted", "excluded_from_ref"})


def valid_quote_filters() -> list:
    """Return filter clauses for valid historical quotes.

    Caller must have Supplier joined (Quote.supplier_id == Supplier.id) before
    applying these filters.
    """
    return [
        Supplier.merge_status == "active",
        (Quote.bid_status.is_(None) | Quote.bid_status.notin_(_EXCLUDED_BID_STATUSES)),
    ]


def valid_quote_query(db: Session):
    """Base query for valid historical quotes with Supplier already joined."""
    return (
        db.query(Quote)
        .join(Supplier, Quote.supplier_id == Supplier.id)
        .filter(*valid_quote_filters())
    )
