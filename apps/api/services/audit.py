"""Domain audit event helper (P1-4 structured audit trail).

Usage:
    write_domain_event(db, user="system", event_type="bql_confirm",
                       identity={"project_id": 1, "submission_id": 5},
                       after={"line_count": 24})
    db.commit()   # caller commits — this function does NOT auto-commit

Payload shape stored in OperationLog.payload (JSON):
    {
      "event_type": str,           # event identifier (see EVENT_TYPES)
      "identity": {                # domain entities involved
          "project_id": int | None,
          "submission_id": int | None,
          "session_id": int | None,
          "alignment_item_id": int | None,
          "alignment_group_id": int | None,
          "finalization_id": int | None,
      },
      "before": dict | None,       # None for creation events
      "after": dict | None,        # field snapshot after change
      "meta": dict,                # event-specific extras (counts, flags)
    }

Canonical RowType vocabulary for BidQuoteLine.row_type (§11.4 resolution):
    quote_line | section_header | remark | invalid | subtotal | grand_total
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.models.operation_log import OperationLog

# Canonical event type identifiers
EVENT_BQL_CONFIRM = "bql_confirm"
EVENT_TENDER_SESSION_CONFIRM = "tender_session_confirm"
EVENT_ALIGNMENT_GROUP_CONFIRM = "alignment_group_confirm"
EVENT_ALIGNMENT_ITEM_CONFIRM = "alignment_item_confirm"
EVENT_ALIGNMENT_BULK_CONFIRM = "alignment_bulk_confirm"
EVENT_ALIGNMENT_FINALIZE = "alignment_finalize"
EVENT_LLM_FILL_PERSIST = "llm_fill_persist"

# Canonical RowType vocabulary (§11.4): maps legacy/dual-enum names → canonical
_ROW_TYPE_NORMALIZE: dict[str, str] = {
    "header": "section_header",
    "note": "remark",
    "empty": "invalid",
}


def normalize_row_type(raw: str | None) -> str:
    """Normalize DraftRow.row_type to the canonical BidQuoteLine vocabulary."""
    if not raw:
        return "quote_line"
    return _ROW_TYPE_NORMALIZE.get(raw, raw)


def _identity_label(identity: dict) -> str:
    """Short human-readable label for OperationLog.target."""
    parts = []
    if identity.get("project_id"):
        parts.append(f"proj={identity['project_id']}")
    if identity.get("submission_id"):
        parts.append(f"sub={identity['submission_id']}")
    if identity.get("session_id"):
        parts.append(f"sess={identity['session_id']}")
    if identity.get("alignment_group_id"):
        parts.append(f"grp={identity['alignment_group_id']}")
    if identity.get("alignment_item_id"):
        parts.append(f"item={identity['alignment_item_id']}")
    if identity.get("finalization_id"):
        parts.append(f"fin={identity['finalization_id']}")
    return " ".join(parts) or "unknown"


def write_domain_event(
    db: Session,
    *,
    user: str,
    event_type: str,
    identity: dict,
    before: dict | None = None,
    after: dict | None = None,
    meta: dict | None = None,
    result: str = "成功",
) -> OperationLog:
    """Append a structured domain audit event to OperationLog.

    Does NOT commit — caller is responsible for committing alongside the
    main business write in the same transaction.
    """
    payload = {
        "event_type": event_type,
        "identity": identity,
        "before": before,
        "after": after,
        "meta": meta or {},
    }
    entry = OperationLog(
        user=user or "system",
        module="bid-compare",
        action=event_type,
        target=_identity_label(identity),
        result=result,
        payload=payload,
    )
    db.add(entry)
    return entry
