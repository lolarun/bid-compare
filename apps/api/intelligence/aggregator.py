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

# Organisation-name suffixes that mark a real bidding company (vs. a product brand).
_COMPANY_SUFFIXES = (
    "公司", "集团", "厂", "有限", "股份", "经营部", "商行", "贸易",
    "实业", "工程", "设备", "科技", "中心", "门市部", "经销", "商贸", "物资",
)


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

        # ── 2b. Smarter supplier_name pick: prefer a company name, never a brand ──
        # OCR on bid documents that quote a single brand (e.g. KITZ, 伯尔梅特)
        # often mislabels that brand as supplier_name. Re-pick from all candidate
        # values, preferring one that looks like a company and is not a brand seen
        # in the line items. (See feedback 2026-06-06.)
        if doc_type == "quote":
            candidates = []
            for p in partials:
                v = (p.data or {}).get("supplier_name") or ""
                v = v.strip() if isinstance(v, str) else ""
                if v and v not in candidates:
                    candidates.append(v)
            item_brands = {
                (it.get("brand") or "").strip()
                for it in all_items if isinstance(it, dict)
            }
            item_brands.discard("")
            merged_data["supplier_name"] = _pick_supplier_name(candidates, item_brands)

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
def _pick_supplier_name(candidates: list[str], item_brands: set[str]) -> str:
    """Choose the best supplier_name, avoiding product brands.

    Priority:
      1. A candidate with a company suffix that is NOT a line-item brand.
      2. Any candidate that is NOT a line-item brand (first such, page order).
      3. Empty string — every candidate is just a brand, so refuse to use it
         as the company name (a blank is safer than polluting the supplier
         master with a brand; the user fills it in on review).
    """
    if not candidates:
        return ""
    for c in candidates:
        if c not in item_brands and any(s in c for s in _COMPANY_SUFFIXES):
            return c
    for c in candidates:
        if c not in item_brands:
            return c
    return ""


def _item_key(item: dict, doc_type: str) -> str:
    """Stable dedup key for an extracted item."""
    if doc_type == "tender":
        return f"{item.get('name', '')}|{item.get('spec', '')}|{item.get('quantity', '')}"
    else:
        return f"{item.get('material', '')}|{item.get('spec', '')}|{item.get('qty', '')}"
