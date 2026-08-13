import json, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(str(ROOT / "data" / "mempas.db"))
cur = conn.cursor()
cur.execute("""
    SELECT bql.id, bql.raw_name, bql.spec, bql.qty,
           bql.unit_price, bql.unit_price_excl_tax, bql.total_price, bql.tax_rate,
           bql.extraction_meta
    FROM bid_quote_lines bql
    WHERE bql.submission_id = 18
    ORDER BY bql.id
""")
rows = cur.fetchall()
conn.close()
print(f"Total BQLs for sub18: {len(rows)}")
print(f"{'id':>5} {'name':<25} {'spec':<18} {'qty':>6} {'u_excl':>10} {'u_incl':>10} {'total':>12} {'tax':>5}  source")
print("-" * 115)
total_price = 0
for r in rows:
    bid_id, name, spec, qty, unit_incl, unit_excl, total, tax, meta_json = r
    meta = json.loads(meta_json) if meta_json else {}
    src = meta.get("source_ref", "")
    name_s = (name or "")[:25]
    spec_s = (spec or "")[:18]
    t = total or 0
    total_price += t
    print(f"{bid_id:>5} {name_s:<25} {spec_s:<18} {qty or 0:>6.1f} {unit_excl or 0:>10.2f} {unit_incl or 0:>10.2f} {t:>12.2f} {tax or 0:>5.2f}  {src}")
print(f"\nTotal: {total_price:,.2f}")
