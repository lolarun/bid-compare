"""ResultAggregator — merge partial ExtractionResponse results from batched pages.

When a multi-page document is split into N batches, each batch produces an
ExtractionResponse with a partial data dict. The aggregator:

  1. Concatenates all items lists (deduplication by identity, not fuzzy).
  2. Takes the FIRST non-empty string for scalar metadata fields (supplier_name,
     project_name, quote_date, etc.) — the first batch usually contains the
     document header; later batches may have empty/repeated metadata.
  3. Sums token usage across batches.
  4. Takes the maximum duration across batches (batches run sequentially but
     the wall-clock time is dominated by the slowest call).
  5. Sets confidence to the minimum across batches (weakest link).

Supported doc_type values: "tender", "quote"
"""

from __future__ import annotations

import time
from typing import Any

from apps.api.intelligence.base import ExtractionResponse


# Scalar metadata fields per document type (ordered by priority — first non-empty wins)
_TENDER_SCALARS = ("project_name", "project_code", "tender_date", "deadline")
_QUOTE_SCALARS  = ("supplier_name", "quote_date")


class ResultAggregator:
    """Stateless utility — merge a list of ExtractionResponse into one."""

    @staticmethod
    def merge(
        partials: list[ExtractionResponse],
        doc_type: str,
    ) -> ExtractionResponse:
        """Merge *partials* produced by batched page processing.

        Args:
            partials: non-empty list of ExtractionResponse objects.
            doc_type: ``"tender"`` or ``"quote"``.

        Returns:
            A single ExtractionResponse with combined data.

        Raises:
            ValueError: if *partials* is empty or *doc_type* is unknown.
        """
        if not partials:
            raise ValueError("partials must not be empty")
        if doc_type not in {"tender", "quote"}:
            raise ValueError(f"Unknown doc_type: {doc_type!r}")

        scalar_keys = _TENDER_SCALARS if doc_type == "tender" else _QUOTE_SCALARS
        item_key    = "items"

        # ── 1. Gather scalar metadata: first non-empty value wins ──────────
        merged_data: dict[str, Any] = {}
        for key in scalar_keys:
            for p in partials:
                val = (p.data or {}).get(key) or ""
                if isinstance(val, str):
                    val = val.strip()
                if val:
                    merged_data[key] = val
                    break
            else:
                merged_data[key] = ""

        # ── 2. Concatenate items from all batches ──────────────────────────
        all_items: list[dict] = []
        seen: set[str] = set()          # rough dedup by (material/name + spec)
        for p in partials:
            for item in (p.data or {}).get(item_key) or []:
                if not isinstance(item, dict):
                    continue
                key_str = _item_key(item, doc_type)
                if key_str and key_str in seen:
                    continue            # skip exact duplicate from context overlap
                if key_str:
                    seen.add(key_str)
                all_items.append(item)
        merged_data[item_key] = all_items

        # ── 3. Carry over quote's context field ───────────────────────────
        if doc_type == "quote":
            for p in partials:
                ctx = (p.data or {}).get("context")
                if ctx:
                    merged_data["context"] = ctx
                    break

        # ── 4. Aggregate numeric / provider metadata ───────────────────────
        tokens  = sum(p.tokens_used or 0 for p in partials)
        dur_ms  = sum(p.duration_ms or 0 for p in partials)
        confs   = [p.confidence for p in partials if p.confidence is not None]
        conf    = min(confs) if confs else None
        raw_texts = [p.raw_text or "" for p in partials if p.raw_text]
        raw_combined = "\n---batch---\n".join(raw_texts)

        return ExtractionResponse(
            data=merged_data,
            raw_text=raw_combined,
            confidence=conf,
            tokens_used=tokens,
            provider=partials[0].provider,
            duration_ms=dur_ms,
            metadata={
                "batches": len(partials),
                "items_per_batch": [
                    len((p.data or {}).get(item_key) or []) for p in partials
                ],
            },
        )


# ─── helpers ──────────────────────────────────────────────────────────────
def _item_key(item: dict, doc_type: str) -> str:
    """Stable dedup key for an extracted item."""
    if doc_type == "tender":
        return f"{item.get('name', '')}|{item.get('spec', '')}|{item.get('quantity', '')}"
    else:
        return f"{item.get('material', '')}|{item.get('spec', '')}|{item.get('qty', '')}"
