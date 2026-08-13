"""Quick inspection of imported data quality."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

print("=== 各品类前5条 Material+Quote 样本 ===")
cats = [r[0] for r in db.execute(text("SELECT DISTINCT category FROM materials ORDER BY category")).fetchall()]

for cat in cats:
    rows = db.execute(text("""
        SELECT m.material_code, m.standard_name, m.spec, q.brand, q.unit_price, q.unit_price_excl_tax
        FROM materials m
        JOIN quotes q ON q.material_id = m.id
        WHERE m.category = :cat
        ORDER BY m.id
        LIMIT 5
    """), {"cat": cat}).fetchall()
    print(f"\n[{cat}]")
    for r in rows:
        print(f"  code={r[0]}  name={r[1][:30] if r[1] else ''}  spec={r[2][:20] if r[2] else ''}  brand={r[3][:15] if r[3] else ''}  price={r[4]}  excl={r[5]}")

# 配電箱 specific: look at bad records
print("\n=== 配电箱 异常价格 (price=1) ===")
bad = db.execute(text("""
    SELECT m.standard_name, m.spec, q.brand, q.unit_price
    FROM materials m JOIN quotes q ON q.material_id = m.id
    WHERE m.category = '配电箱' AND q.unit_price = 1
    LIMIT 10
""")).fetchall()
for r in bad:
    print(f"  name={r[0][:40]}  spec={r[1]}  brand={r[2]}  price={r[3]}")

print("\n=== 配电箱 正常价格样本 ===")
good = db.execute(text("""
    SELECT m.standard_name, m.spec, q.brand, q.unit_price
    FROM materials m JOIN quotes q ON q.material_id = m.id
    WHERE m.category = '配电箱' AND q.unit_price > 100
    LIMIT 10
""")).fetchall()
for r in good:
    print(f"  name={r[0][:40]}  brand={r[2]}  price={r[3]}")

db.close()
