"""scripts/retarget_sub1819.py

Targeted re-extraction for sub18 (泰科龙) and sub19 (凯硕新正).

Reads existing OCR HTML from tmp_p*.html files (already OCR'd),
calls LLM with updated prompt, compares with DB, applies corrections.

Usage:
    python scripts/retarget_sub1819.py [--dry-run] [--sub18] [--sub19]
"""
from __future__ import annotations
import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv("apps/api/.env")

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

from apps.api.core.database import SessionLocal
from apps.api.models.bid_submission import BidQuoteLine, BidSubmission
from apps.api.intelligence.providers.dashscope_ocr import DashScopeOCRProvider

# ── helpers ──────────────────────────────────────────────────────────────────

def read_html(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    # strip markdown code fence if present
    if content.startswith("```"):
        lines = content.splitlines()
        lines = [l for l in lines if l not in ("```html", "```", "```json")]
        content = "\n".join(lines).strip()
    return content


def extract_page(provider: DashScopeOCRProvider, html: str, page_num: int) -> list[dict]:
    """Run LLM extraction on raw HTML. Returns list of item dicts."""
    data, _, tokens = provider._llm_parse(html, "quote")
    log.info("  page %d: LLM returned %d items (%d tokens)", page_num,
             len(data.get("items", [])), tokens)
    items = data.get("items", [])
    # Tag each item with page source_ref
    for item in items:
        item.setdefault("source_ref", {"page": page_num})
    items = fix_arithmetic_items(items)
    return items


def coerce(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("，", ""))
    except (ValueError, TypeError):
        return None


def fix_arithmetic_items(items: list[dict]) -> list[dict]:
    """Post-processing: fix two known LLM mis-extraction patterns.

    Pattern A — same-value bug: LLM sets unit_price == unit_price_excl_tax
    (both equal the excl-tax value) but qty × excl_tax × (1+tax) ≈ total_price.
    Fix: unit_price = unit_price_excl_tax × (1+tax_rate).

    Pattern B — OCR qty misread: qty × unit_price ≠ total_price but
    total_price / unit_price is a clean integer → that integer is the real qty;
    also detects when unit_price is excl-tax in this case and inflates it.
    """
    for item in items:
        up = coerce(item.get("unit_price"))
        up_excl = coerce(item.get("unit_price_excl_tax"))
        tp = coerce(item.get("total_price"))
        qty = coerce(item.get("qty"))
        tax = coerce(item.get("tax_rate")) or 0.0

        if up is None or tp is None or qty is None or qty == 0:
            continue

        dev = abs(qty * up - tp) / max(abs(tp), 1.0)

        # Pattern A: up == up_excl (same value) AND qty × up × (1+tax) ≈ tp
        if (up_excl is not None and abs(up - up_excl) < 0.02 and tax > 0
                and abs(qty * up * (1 + tax) - tp) / max(abs(tp), 1.0) < 0.02):
            incl = round(up * (1 + tax), 2)
            log.info("  fix-A: %s %s  unit_price %s→%s (excl-tax→incl-tax)",
                     item.get("material", ""), item.get("spec", ""), up, incl)
            item["unit_price"] = incl
            # unit_price_excl_tax stays at up (correct excl-tax value)
            item["_fix"] = "pattern_A"
            continue

        # Pattern B: qty × up ≠ tp by >5%, but tp / up is a near-integer
        if dev > 0.05 and up > 0:
            ratio = tp / up
            derived_qty = round(ratio)
            if derived_qty > 0 and abs(ratio - derived_qty) < 0.02:
                # Does up look like an already-correct 含税 price?
                up_appears_incl = (
                    up_excl is not None and tax > 0
                    and abs(up_excl * (1 + tax) - up) / max(abs(up), 1.0) < 0.02
                )
                if up_appears_incl and derived_qty == 1:
                    # LLM got qty wrong, but prices already correct — just fix qty + total
                    incl_tp = round(float(derived_qty) * up, 2)
                    log.info("  fix-B(qty-only): %s %s  qty %s→%s",
                             item.get("material", ""), item.get("spec", ""),
                             qty, derived_qty)
                    item["qty"] = derived_qty
                    item["total_price"] = incl_tp
                    item["_fix"] = "pattern_B_qty_only"
                else:
                    # tp is excl-tax total, up is excl-tax unit price → inflate to 含税
                    incl_up = round(up * (1 + tax), 2) if tax > 0 else up
                    incl_tp = round(float(derived_qty) * incl_up, 2)
                    log.info("  fix-B: %s %s  qty %s→%s  unit_price %s→%s  total %s→%s",
                             item.get("material", ""), item.get("spec", ""),
                             qty, derived_qty, up, incl_up, tp, incl_tp)
                    item["qty"] = derived_qty
                    item["unit_price_excl_tax"] = up
                    item["unit_price"] = incl_up
                    item["total_price"] = incl_tp
                    item["_fix"] = "pattern_B"

    return items


# ── sub18 (泰科龙) pages ──────────────────────────────────────────────────────

SUB18_PAGES = {
    6: "tmp_p6.html",
    7: "tmp_p7.html",
    8: "tmp_p8.html",
    9: "tmp_p9.html",
    10: "tmp_p10.html",
    11: "tmp_p11.html",
    12: "tmp_p12.html",
}


def retarget_sub18(provider: DashScopeOCRProvider, db, dry_run: bool):
    sub = db.query(BidSubmission).filter(BidSubmission.id == 18).first()
    if not sub:
        log.error("sub18 not found")
        return

    log.info("=== sub18 (泰科龙) ===")
    log.info("Current BQL count: %d", db.query(BidQuoteLine).filter(
        BidQuoteLine.submission_id == 18).count())

    all_items: list[dict] = []
    for page_num, html_file in sorted(SUB18_PAGES.items()):
        if not os.path.exists(html_file):
            log.warning("  HTML file not found: %s", html_file)
            continue
        log.info("  Processing %s (page %d)...", html_file, page_num)
        html = read_html(html_file)
        items = extract_page(provider, html, page_num)
        all_items.extend(items)
        for it in items:
            log.info("    item: %s %s qty=%s up=%s tp=%s",
                     it.get("material", ""), it.get("spec", ""),
                     it.get("qty"), it.get("unit_price"), it.get("total_price"))

    # Save extracted items to file for review
    out_path = "tmp_sub18_reextract.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)
    log.info("Saved %d items to %s", len(all_items), out_path)

    if dry_run:
        log.info("DRY RUN: skipping DB update for sub18")
        return

    # Fetch ALL current BQL records for sub18
    existing = db.query(BidQuoteLine).filter(
        BidQuoteLine.submission_id == 18
    ).order_by(BidQuoteLine.id).all()

    # Strategy: find records from pages 6/9/11/12 by matching page source_ref
    # For those pages, replace with re-extracted items.
    # Records from page 5 (TableGrid, correct) stay untouched.
    page5_ids = set()
    other_page_ids = set()
    for bql in existing:
        meta = bql.extraction_meta or {}
        sr = meta.get("source_ref")
        if sr and sr.get("page") == 5:
            page5_ids.add(bql.id)
        else:
            other_page_ids.add(bql.id)

    log.info("Page 5 records (keep): %d  |  Other records (replace): %d",
             len(page5_ids), len(other_page_ids))

    # Delete records from other pages
    for bql in existing:
        if bql.id in other_page_ids:
            db.delete(bql)

    # Insert new records from re-extraction
    job_id = None
    if existing:
        meta = existing[0].extraction_meta or {}
        job_id = meta.get("extraction_job_id")

    for it in all_items:
        meta_json = {
            "extraction_job_id": job_id,
            "source_ref": it.get("source_ref"),
            "raw_material": it.get("material", ""),
            "raw_spec": it.get("spec", ""),
            "raw_unit": it.get("unit", ""),
            "raw_remark": it.get("remark", ""),
            "material_type": it.get("material_type", ""),
            "canonical": it.get("canonical", {}),
            "validation_warning": it.get("validation_warning", ""),
            "normalized_material": it.get("normalized_material", ""),
            "ocr_correction_reason": it.get("ocr_correction_reason", ""),
        }
        bql = BidQuoteLine(
            submission_id=18,
            raw_name=it.get("normalized_material") or it.get("material", ""),
            spec=it.get("spec", ""),
            brand=it.get("brand", ""),
            unit=it.get("unit", ""),
            qty=coerce(it.get("qty")),
            unit_price=coerce(it.get("unit_price")),
            unit_price_excl_tax=coerce(it.get("unit_price_excl_tax")),
            tax_rate=coerce(it.get("tax_rate")),
            total_price=coerce(it.get("total_price")),
            extraction_meta=meta_json,
        )
        db.add(bql)

    db.commit()
    new_count = db.query(BidQuoteLine).filter(BidQuoteLine.submission_id == 18).count()
    log.info("sub18 updated: %d total BQL records", new_count)


# ── sub19 (凯硕新正) pages ────────────────────────────────────────────────────

SUB19_PAGES = {
    4: "tmp_sub19_p4.html",
    5: "tmp_sub19_p5.html",
    6: "tmp_sub19_p6.html",
    7: "tmp_sub19_p7.html",
}


def retarget_sub19(provider: DashScopeOCRProvider, db, dry_run: bool):
    sub = db.query(BidSubmission).filter(BidSubmission.id == 19).first()
    if not sub:
        log.error("sub19 not found")
        return

    log.info("=== sub19 (凯硕新正) ===")
    log.info("Current BQL count: %d", db.query(BidQuoteLine).filter(
        BidQuoteLine.submission_id == 19).count())

    all_items: list[dict] = []
    for page_num, html_file in sorted(SUB19_PAGES.items()):
        if not os.path.exists(html_file):
            log.warning("  HTML file not found: %s", html_file)
            continue
        log.info("  Processing %s (page %d)...", html_file, page_num)
        html = read_html(html_file)
        items = extract_page(provider, html, page_num)
        all_items.extend(items)
        for it in items:
            log.info("    item: %s %s qty=%s up=%s up_excl=%s tp=%s",
                     it.get("material", ""), it.get("spec", ""),
                     it.get("qty"), it.get("unit_price"),
                     it.get("unit_price_excl_tax"), it.get("total_price"))

    out_path = "tmp_sub19_reextract.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)
    log.info("Saved %d items to %s", len(all_items), out_path)

    if dry_run:
        log.info("DRY RUN: skipping DB update for sub19")
        return

    # Delete all existing BQL records for sub19 and re-insert from full re-extraction
    # (sub19 price table spans pages 4-7; page 3 is a non-price/cover page)
    existing = db.query(BidQuoteLine).filter(
        BidQuoteLine.submission_id == 19
    ).order_by(BidQuoteLine.id).all()

    # Preserve source_ref for records already having it (page 4 items had source_ref)
    # but since we're re-extracting page 4 too, just replace all
    job_id = None
    if existing:
        meta = existing[0].extraction_meta or {}
        job_id = meta.get("extraction_job_id")

    for bql in existing:
        db.delete(bql)

    for it in all_items:
        meta_json = {
            "extraction_job_id": job_id,
            "source_ref": it.get("source_ref"),
            "raw_material": it.get("material", ""),
            "raw_spec": it.get("spec", ""),
            "raw_unit": it.get("unit", ""),
            "raw_remark": it.get("remark", ""),
            "material_type": it.get("material_type", ""),
            "canonical": it.get("canonical", {}),
            "validation_warning": it.get("validation_warning", ""),
            "normalized_material": it.get("normalized_material", ""),
            "ocr_correction_reason": it.get("ocr_correction_reason", ""),
        }
        bql = BidQuoteLine(
            submission_id=19,
            raw_name=it.get("normalized_material") or it.get("material", ""),
            spec=it.get("spec", ""),
            brand=it.get("brand", ""),
            unit=it.get("unit", ""),
            qty=coerce(it.get("qty")),
            unit_price=coerce(it.get("unit_price")),
            unit_price_excl_tax=coerce(it.get("unit_price_excl_tax")),
            tax_rate=coerce(it.get("tax_rate")),
            total_price=coerce(it.get("total_price")),
            extraction_meta=meta_json,
        )
        db.add(bql)

    db.commit()
    new_count = db.query(BidQuoteLine).filter(BidQuoteLine.submission_id == 19).count()
    log.info("sub19 updated: %d total BQL records", new_count)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Retarget sub18/sub19 extraction")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and show results but don't update DB")
    parser.add_argument("--sub18", action="store_true", help="Process sub18 only")
    parser.add_argument("--sub19", action="store_true", help="Process sub19 only")
    args = parser.parse_args()

    do_both = not args.sub18 and not args.sub19

    provider = DashScopeOCRProvider()
    db = SessionLocal()

    try:
        if args.sub18 or do_both:
            retarget_sub18(provider, db, dry_run=args.dry_run)
        if args.sub19 or do_both:
            retarget_sub19(provider, db, dry_run=args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
