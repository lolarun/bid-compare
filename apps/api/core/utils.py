"""Cross-cutting utilities — no business logic, no DB access."""

from __future__ import annotations

from fastapi import HTTPException


def parse_id_csv(value: str, field_name: str = "ids") -> list[int]:
    """Parse a comma-separated integer string into list[int].

    Raises HTTP 400 with a descriptive message on parse failure.
    Returns an empty list when value is empty or whitespace-only.
    """
    if not value or not value.strip():
        return []
    try:
        return [int(x) for x in value.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, f"{field_name} 须为逗号分隔的整数")
