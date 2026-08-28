"""Audit Material master data vs. historical quotes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

print("=== Material 表总体 ===")
total_mat = db.execute(text("SELECT COUNT(*) FROM materials")).scalar()
total_q   = db.execute(text("SELECT COUNT(*) FROM quotes")).scalar()
print(f"  materials: {total_mat},  quotes: {total_q}")
print(f"  material:quote 比 = 1:{total_q//total_mat if total_mat else '?'}")

print()
print("=== 每条 Material 绑定几条 Quote ===")
rows = db.execute(text("""
    SELECT q_count, COUNT(*) mat_count
    FROM (
        SELECT m.id, COUNT(q.id) q_count
        FROM materials m LEFT JOIN quotes q ON q.material_id = m.id
        GROUP BY m.id
    )
    GROUP BY q_count ORDER BY q_count
""")).fetchall()
for r in rows:
    print(f"  {r[0]} quote/material → {r[1]} materials")

print()
print("=== 各品类重复名称+规格组合数 ===")
rows2 = db.execute(text("""
    SELECT m.category,
           COUNT(*) total_mat,
           COUNT(DISTINCT m.standard_name || '||' || COALESCE(m.spec,'')) uniq_name_spec,
           COUNT(*) - COUNT(DISTINCT m.standard_name || '||' || COALESCE(m.spec,'')) duplicates
    FROM materials m
    GROUP BY m.category
    ORDER BY duplicates DESC
""")).fetchall()
print(f"  {'品类':8s}  {'材料数':>6s}  {'唯一名称+规格':>12s}  {'重复数':>6s}")
print("  " + "-"*42)
for r in rows2:
    print(f"  {r[0]:8s}  {r[1]:>6d}  {r[2]:>12d}  {r[3]:>6d}")

print()
print("=== 重复最多的 name+spec 组合（前20）===")
rows3 = db.execute(text("""
    SELECT m.category, m.standard_name, COALESCE(m.spec,'') spec,
           COUNT(*) cnt,
           MIN(q.unit_price) price_min,
           MAX(q.unit_price) price_max,
           COUNT(DISTINCT COALESCE(q.brand,'')) brand_cnt
    FROM materials m JOIN quotes q ON q.material_id = m.id
    GROUP BY m.category, m.standard_name, m.spec
    HAVING cnt > 1
    ORDER BY cnt DESC
    LIMIT 20
""")).fetchall()
print(f"  {'品类':8s}  {'名称':25s}  {'规格':15s}  {'cnt':>4s}  {'price_min':>9s}  {'price_max':>9s}  {'brands':>6s}")
print("  " + "-"*85)
for r in rows3:
    print(f"  {r[0]:8s}  {r[1][:24]:24s}  {r[2][:14]:14s}  {r[3]:>4d}  {r[4]:>9.1f}  {r[5]:>9.1f}  {r[6]:>6d}")

db.close()
