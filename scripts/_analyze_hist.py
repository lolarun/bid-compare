"""分析历史采购数据中的供应商-品牌、供应商-品类关系。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.api.core.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()

# ── 1. 品牌列等于供应商名称的比例 ───────────────────────────────────────────
total    = db.execute(text("SELECT COUNT(*) FROM quotes WHERE supplier_id IS NOT NULL")).scalar()
same     = db.execute(text("""
    SELECT COUNT(*) FROM quotes q
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE q.brand = s.name
""")).scalar()
print(f"brand == supplier name: {same}/{total} ({same*100//total if total else 0}%)")
print()

# ── 2. 空调泵品牌列是功率值（非品牌）───────────────────────────────────────
print("=== 疑似非品牌值（数字+单位）进入 Supplier 表 ===")
rows_kw = db.execute(text("""
    SELECT s.name, m.category, COUNT(*) cnt
    FROM quotes q
    JOIN materials m ON q.material_id = m.id
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE s.name GLOB '*KW' OR s.name GLOB '*kw' OR s.name GLOB '*KVA'
    GROUP BY s.name, m.category
    ORDER BY m.category, s.name
""")).fetchall()
for r in rows_kw:
    print(f"  [{r[1]:8s}] supplier={r[0]:12s}  n={r[2]}")
print()

# ── 3. 真实供应商-品类矩阵（排除功率值） ──────────────────────────────────
print("=== 供应商 × 品类报价矩阵（排除 *KW/*KVA 类噪声） ===")
rows = db.execute(text("""
    SELECT s.name, m.profession, m.category, COUNT(*) cnt,
           ROUND(MIN(q.unit_price),2) price_min,
           ROUND(MAX(q.unit_price),2) price_max
    FROM quotes q
    JOIN materials m ON q.material_id = m.id
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE s.name NOT GLOB '*KW' AND s.name NOT GLOB '*kw' AND s.name NOT GLOB '*KVA'
    GROUP BY s.name, m.category
    ORDER BY m.category, cnt DESC
""")).fetchall()

current_cat = None
for r in rows:
    cat = r[2]
    if cat != current_cat:
        print(f"\n  [{r[1]} / {cat}]")
        current_cat = cat
    print(f"    {r[0][:28]:28s}  n={r[3]:4d}  CNY[{r[4]:.0f}~{r[5]:.0f}]")

# ── 4. 各品类有效供应商数量汇总 ──────────────────────────────────────────
print()
print("=== 各品类有效供应商数（排除噪声后）===")
rows2 = db.execute(text("""
    SELECT m.category, m.profession,
           COUNT(DISTINCT s.id) sup_cnt,
           COUNT(*) quote_cnt
    FROM quotes q
    JOIN materials m ON q.material_id = m.id
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE s.name NOT GLOB '*KW' AND s.name NOT GLOB '*kw' AND s.name NOT GLOB '*KVA'
    GROUP BY m.category
    ORDER BY sup_cnt DESC
""")).fetchall()
print(f"  {'品类':8s}  {'专业':6s}  {'供应商数':>5s}  {'报价行数':>6s}")
print("  " + "-" * 35)
for r in rows2:
    print(f"  {r[0]:8s}  {r[1]:6s}  {r[2]:>5d}  {r[3]:>6d}")

# ── 5. 供应商跨品类覆盖（同一供应商出现在多个品类）────────────────────────
print()
print("=== 跨品类供应商（出现在 ≥2 个品类的供应商）===")
rows3 = db.execute(text("""
    SELECT s.name,
           GROUP_CONCAT(DISTINCT m.category) cats,
           COUNT(DISTINCT m.category) cat_cnt,
           COUNT(*) total_quotes
    FROM quotes q
    JOIN materials m ON q.material_id = m.id
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE s.name NOT GLOB '*KW' AND s.name NOT GLOB '*kw' AND s.name NOT GLOB '*KVA'
    GROUP BY s.id
    HAVING cat_cnt >= 2
    ORDER BY cat_cnt DESC, total_quotes DESC
""")).fetchall()
for r in rows3:
    print(f"  {r[0][:24]:24s}  cats={r[2]}  [{r[1]}]  n={r[3]}")

# ── 6. 品牌≠供应商的案例（真正的代理关系信号）── 当前数据是否存在
print()
print("=== brand != supplier name 的记录（代理关系信号）===")
rows4 = db.execute(text("""
    SELECT s.name, q.brand, m.category, COUNT(*) cnt
    FROM quotes q
    JOIN materials m ON q.material_id = m.id
    JOIN suppliers s ON q.supplier_id = s.id
    WHERE q.brand != s.name AND q.brand != '' AND q.brand IS NOT NULL
      AND s.name NOT GLOB '*KW' AND s.name NOT GLOB '*kw'
    GROUP BY s.name, q.brand, m.category
    ORDER BY cnt DESC
""")).fetchall()
if rows4:
    for r in rows4:
        print(f"  supplier={r[0][:20]:20s}  brand={r[1][:20]:20s}  [{r[2]}]  n={r[3]}")
else:
    print("  (无) 本批数据中品牌列即为供应商名称，无供应商代理他牌的记录")

db.close()
