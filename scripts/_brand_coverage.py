import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
rows = db.execute(text("""
    SELECT m.category,
           COUNT(*) total,
           SUM(CASE WHEN q.brand IS NULL OR q.brand = '' THEN 1 ELSE 0 END) blank,
           COUNT(*) - SUM(CASE WHEN q.brand IS NULL OR q.brand = '' THEN 1 ELSE 0 END) has_brand
    FROM materials m JOIN quotes q ON q.material_id = m.id
    GROUP BY m.category ORDER BY blank DESC
""")).fetchall()
print(f"  {'品类':8s}  {'总计':>5s}  {'有品牌':>6s}  {'无品牌':>6s}  {'覆盖率':>6s}")
print("  " + "-"*40)
for r in rows:
    pct = r[3]*100//r[1] if r[1] else 0
    print(f"  {r[0]:8s}  {r[1]:>5d}  {r[3]:>6d}  {r[2]:>6d}  {pct:>5d}%")
db.close()
