import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

rows = db.execute(text("""
    SELECT category,
           COUNT(*) total,
           SUM(CASE WHEN tier='合资' THEN 1 ELSE 0 END) joint,
           SUM(CASE WHEN alias_of IS NOT NULL THEN 1 ELSE 0 END) aliases
    FROM brand_tiers WHERE is_approved=1
    GROUP BY category ORDER BY category
""")).fetchall()
print(f"  {'品类':16s}  {'总计':>4s}  {'合资':>4s}  {'别名':>4s}")
print("  " + "-"*34)
for r in rows:
    print(f"  {r[0]:16s}  {r[1]:>4d}  {r[2]:>4d}  {r[3]:>4d}")

aliases = db.execute(text(
    "SELECT brand_name, canonical_name FROM brand_tiers WHERE alias_of IS NOT NULL"
)).fetchall()
print("\n别名记录:")
for a in aliases:
    print(f"  {a[0]} → {a[1]}")
db.close()
