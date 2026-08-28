import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

print("=== Quote.supplier_id 分布 ===")
total = db.execute(text("SELECT COUNT(*) FROM quotes")).scalar()
null_sup = db.execute(text("SELECT COUNT(*) FROM quotes WHERE supplier_id IS NULL")).scalar()
print(f"  total: {total}, supplier_id IS NULL: {null_sup} ({null_sup*100//total}%)")

print()
print("=== 各品类基准价是否生成 ===")
rows = db.execute(text("""
    SELECT m.category, COUNT(*) mats,
           SUM(CASE WHEN m.ref_price_avg IS NOT NULL THEN 1 ELSE 0 END) with_baseline
    FROM materials m
    GROUP BY m.category
    ORDER BY m.category
""")).fetchall()
for r in rows:
    print(f"  {r[0]:8s}  materials={r[1]:5d}  with_baseline={r[2]:5d}  ({r[2]*100//r[1] if r[1] else 0}%)")

db.close()
