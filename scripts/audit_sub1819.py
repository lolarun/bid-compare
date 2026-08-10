"""scripts/audit_sub1819.py

Audit report: backup (pre-fix) vs current DB for sub18 and sub19.
Produces row-by-row conservation report and total reconciliation.

Usage:
    python scripts/audit_sub1819.py
"""
import sqlite3
import json
from collections import defaultdict

BAK = "data/mempas-before-repair63-sub1719-20260618-225755.bak"
CUR = "data/mempas.db"

# ── helpers ──────────────────────────────────────────────────────────────────

def query_sub(db_path: str, sub_id: int) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM bid_quote_lines WHERE submission_id=? ORDER BY id",
        (sub_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        meta = json.loads(r["extraction_meta"]) if r["extraction_meta"] else {}
        sr = meta.get("source_ref") or {}
        name_raw = meta.get("raw_material") or r["raw_name"] or ""
        result.append(
            {
                "id": r["id"],
                "name": name_raw,
                "spec": r["spec"] or "",
                "qty": r["qty"],
                "up": r["unit_price"],
                "up_excl": r["unit_price_excl_tax"],
                "tp": r["total_price"],
                "page": sr.get("page"),
                "table": sr.get("table"),
                "row": sr.get("row"),
                "bbox": sr.get("bbox"),
            }
        )
    return result


def audit(sub_id: int, label: str):
    old = query_sub(BAK, sub_id)
    new = query_sub(CUR, sub_id)
    old_total = sum(r["tp"] or 0 for r in old)
    new_total = sum(r["tp"] or 0 for r in new)

    print(f"\n{'='*70}")
    print(f"  {label}  (sub{sub_id})")
    print(f"{'='*70}")
    print(f"  OLD: {len(old)} rows   total = {old_total:>14,.2f}")
    print(f"  NEW: {len(new)} rows   total = {new_total:>14,.2f}")
    print(f"  DELTA:        {len(new)-len(old):>4} rows   delta = {new_total-old_total:>14,.2f}")

    # Page-level breakdown
    old_by_page: dict[object, list] = defaultdict(list)
    new_by_page: dict[object, list] = defaultdict(list)
    for r in old:
        old_by_page[r["page"]].append(r)
    for r in new:
        new_by_page[r["page"]].append(r)

    all_pages = sorted(
        set(list(old_by_page.keys()) + list(new_by_page.keys())),
        key=lambda x: (x is None, x if x is not None else 0),
    )
    print()
    print(f"  {'Page':<6} {'OLD rows':<9} {'OLD total':>14}  {'NEW rows':<9} {'NEW total':>14}  {'delta':>12}")
    for p in all_pages:
        o = old_by_page[p]
        n = new_by_page[p]
        ot = sum(r["tp"] or 0 for r in o)
        nt = sum(r["tp"] or 0 for r in n)
        print(f"  {str(p):<6} {len(o):<9} {ot:>14,.2f}  {len(n):<9} {nt:>14,.2f}  {nt-ot:>12,.2f}")

    # Rows only in OLD (deleted)
    new_key_set = {(r["name"], r["spec"], r["qty"]) for r in new}
    deleted = [
        r for r in old if (r["name"], r["spec"], r["qty"]) not in new_key_set
    ]
    print(f"\n  DELETED rows ({len(deleted)}):")
    for r in deleted:
        print(
            f"    pg={r['page']} tbl={r['table']} row={r['row']}"
            f"  {r['name'][:28]:<28} {r['spec']:<12}"
            f"  qty={r['qty']}  up={r['up']}  tp={r['tp']}"
        )

    # Rows only in NEW (added)
    old_key_set = {(r["name"], r["spec"], r["qty"]) for r in old}
    added = [
        r for r in new if (r["name"], r["spec"], r["qty"]) not in old_key_set
    ]
    print(f"\n  ADDED rows ({len(added)}):")
    for r in added:
        sr_str = f"pg={r['page']} tbl={r['table']} row={r['row']}"
        print(
            f"    {sr_str}  {r['name'][:28]:<28} {r['spec']:<12}"
            f"  qty={r['qty']}  up={r['up']}  tp={r['tp']}"
        )

    # source_ref coverage in NEW
    has_full_sr = sum(
        1 for r in new if r["page"] is not None and r["row"] is not None
    )
    has_page_only = sum(
        1 for r in new if r["page"] is not None and r["row"] is None
    )
    has_none = sum(1 for r in new if r["page"] is None)
    print(f"\n  source_ref coverage (NEW):")
    print(f"    page+table+row: {has_full_sr}/{len(new)}")
    print(f"    page-only:      {has_page_only}/{len(new)}")
    print(f"    none:           {has_none}/{len(new)}")


if __name__ == "__main__":
    audit(18, "sub18 泰科龙")
    audit(19, "sub19 凯硕新正")
