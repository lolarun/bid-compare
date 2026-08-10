import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

checks = [
    ("潜水泵 unit != 台",   "SELECT COUNT(*) FROM materials WHERE category='潜水泵' AND unit!='台'"),
    ("空调泵 unit != 台",   "SELECT COUNT(*) FROM materials WHERE category='空调泵' AND unit!='台'"),
    ("spec 含换行",         "SELECT COUNT(*) FROM materials WHERE spec LIKE '%\n%'"),
    ("unit = '0'",          "SELECT COUNT(*) FROM materials WHERE unit='0'"),
    ("blank name",          "SELECT COUNT(*) FROM materials WHERE standard_name IS NULL OR standard_name=''"),
    ("price <= 0",          "SELECT COUNT(*) FROM quotes WHERE unit_price IS NULL OR unit_price <= 0"),
    ("brand = T01_*",       "SELECT COUNT(*) FROM quotes WHERE brand LIKE 'T01_%'"),
    ("brand 含付款条款",    "SELECT COUNT(*) FROM quotes WHERE brand LIKE '2025%' OR brand LIKE '2024%'"),
]

all_ok = True
for label, sql in checks:
    n = db.execute(text(sql)).scalar()
    status = "OK" if n == 0 else f"ISSUE  n={n}"
    if n:
        all_ok = False
    print(f"  {label:28s}  {status}")

print()
print("All checks passed" if all_ok else "Some issues remain")

# Summary counts
total_mat = db.execute(text("SELECT COUNT(*) FROM materials")).scalar()
total_q   = db.execute(text("SELECT COUNT(*) FROM quotes")).scalar()
blank_unit = db.execute(text("SELECT COUNT(*) FROM materials WHERE unit IS NULL OR unit=''")).scalar()
blank_brand = db.execute(text("SELECT COUNT(*) FROM quotes WHERE brand IS NULL OR brand=''")).scalar()
print(f"\n  materials: {total_mat}, quotes: {total_q}")
print(f"  blank unit: {blank_unit}  blank brand: {blank_brand}")
db.close()
