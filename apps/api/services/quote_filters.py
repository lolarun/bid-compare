"""Shared "valid historical quote" filter — single source of truth.

All reference-price queries MUST call valid_quote_filters() to exclude:
  - Quotes from merged/inactive suppliers when a supplier is linked
    (Quote.supplier_id IS NOT NULL AND Supplier.merge_status != 'active')
  - Quotes with no supplier linkage pass the supplier check automatically —
    brand-only historical quotes do not require an active Supplier record.
  - Quotes explicitly flagged as polluted or excluded from reference prices
    (Quote.bid_status IN ('polluted', 'excluded_from_ref'))

Usage (caller must OUTER-join Supplier before applying):
    q = db.query(Quote).outerjoin(Supplier, Quote.supplier_id == Supplier.id)
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

    Caller must OUTER-join Supplier (Quote.supplier_id == Supplier.id) before
    applying these filters.  Quotes with supplier_id IS NULL pass the supplier
    check so that brand-only historical records are included.
    """
    return [
        (Quote.supplier_id.is_(None) | (Supplier.merge_status == "active")),
        (Quote.bid_status.is_(None) | Quote.bid_status.notin_(_EXCLUDED_BID_STATUSES)),
    ]


def valid_quote_query(db: Session):
    """Base query for valid historical quotes with Supplier already outer-joined."""
    return (
        db.query(Quote)
        .outerjoin(Supplier, Quote.supplier_id == Supplier.id)
        .filter(*valid_quote_filters())
    )
