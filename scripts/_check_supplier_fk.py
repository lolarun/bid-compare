"""Find all tables referencing suppliers.id."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

# Check which tables have supplier_id column
rows = db.execute(text("""
    SELECT m.name, p.name
    FROM sqlite_master m
    JOIN pragma_table_info(m.name) p ON p.name LIKE '%supplier%'
    WHERE m.type = 'table'
    ORDER BY m.name
""")).fetchall()
print("Tables with supplier* column:")
for r in rows:
    cnt = db.execute(text(f"SELECT COUNT(*) FROM {r[0]} WHERE {r[1]} IS NOT NULL")).scalar()
    print(f"  {r[0]}.{r[1]}  non-null count: {cnt}")

db.close()
