import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
rows = db.execute(text("""
    SELECT m.category, m.standard_name, m.spec, m.unit, q.brand, q.unit_price
    FROM materials m JOIN quotes q ON q.material_id = m.id
    WHERE m.unit IS NULL OR m.unit = ''
""")).fetchall()
for r in rows:
    print(f"  [{r[0]}] name={r[1]}  spec={r[2]}  unit={repr(r[3])}  brand={r[4]}  price={r[5]}")
db.close()
