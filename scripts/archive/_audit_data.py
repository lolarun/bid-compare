"""Full audit of imported historical data — surface obvious quality issues."""
import sys, re
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

CATS = [r[0] for r in db.execute(text(
    "SELECT DISTINCT category FROM materials ORDER BY category"
)).fetchall()]

for cat in CATS:
    rows = db.execute(text("""
        SELECT m.material_code, m.standard_name, m.spec, m.unit,
               q.brand, q.unit_price, q.unit_price_excl_tax
        FROM materials m JOIN quotes q ON q.material_id = m.id
        WHERE m.category = :cat
        ORDER BY m.id
    """), {"cat": cat}).fetchall()

    names   = [r[1] or "" for r in rows]
    specs   = [r[2] or "" for r in rows]
    units   = [r[3] or "" for r in rows]
    brands  = [r[4] or "" for r in rows]
    prices  = [r[5] for r in rows]

    # ── anomalies
    blank_name   = sum(1 for n in names if not n.strip() or n == cat)
    blank_spec   = sum(1 for s in specs if not s.strip())
    blank_unit   = sum(1 for u in units if not u.strip())
    blank_brand  = sum(1 for b in brands if not b.strip())
    price_le0    = sum(1 for p in prices if p is None or p <= 0)
    price_too_lo = sum(1 for p in prices if p is not None and 0 < p < 0.5)
    price_too_hi = sum(1 for p in prices if p is not None and p > 10_000_000)

    # name quality checks
    has_label   = [n for n in names if any(x in n for x in ["柜号", "型号", "名称", "序号", "备注"])]
    digit_start = [n for n in names if n and n[0].isdigit()]

    print(f"\n{'='*60}")
    print(f"[{cat}]  n={len(rows)}")
    print(f"  blank name  : {blank_name}  |  blank spec : {blank_spec}  |  blank unit : {blank_unit}")
    print(f"  blank brand : {blank_brand}/{len(rows)}")
    print(f"  price <=0   : {price_le0}  |  price <0.5 : {price_too_lo}  |  price >10M : {price_too_hi}")
    if has_label:
        print(f"  名称含列标签  : {len(has_label)} e.g. {has_label[:2]}")
    if digit_start:
        print(f"  名称以数字开头: {len(digit_start)} e.g. {digit_start[:3]}")

    # unit variety
    unit_counts = Counter(units)
    print(f"  units       : {dict(unit_counts.most_common(6))}")

    # brand variety
    brand_counts = Counter(brands)
    top_brands = brand_counts.most_common(8)
    suspicious = [(b,c) for b,c in top_brands if not b or len(b)>30
                  or any(x in b for x in ["：","序","备注","单价","合计"])]
    print(f"  top brands  : {top_brands}")
    if suspicious:
        print(f"  *** suspicious brands: {suspicious}")

    # price range per name group (top 3 most common names)
    name_prices = {}
    for r in rows:
        n = r[1] or ""
        name_prices.setdefault(n, []).append(r[5])
    for name, ps in sorted(name_prices.items(), key=lambda x: -len(x[1]))[:3]:
        ps2 = [p for p in ps if p and p > 0]
        if ps2:
            print(f"  [{name[:25]:25s}] n={len(ps2):4d}  min={min(ps2):.1f}  max={max(ps2):.1f}")

    # Show first 5 rows
    print(f"  --- first 5 rows ---")
    for r in rows[:5]:
        print(f"    name={r[1][:28]:28s}  spec={r[2][:18] if r[2] else '':18s}  "
              f"brand={r[4][:12] if r[4] else '':12s}  price={r[5]}")

db.close()
