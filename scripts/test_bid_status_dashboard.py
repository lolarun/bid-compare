"""Verify bid_status='未中标' quotes are excluded from heatmap/bubble."""
import sqlite3

conn = sqlite3.connect("data/mempas.db")

# Count total quotes with bid_status='未中标'
total_lost = conn.execute("SELECT COUNT(*) FROM quotes WHERE bid_status = '未中标'").fetchone()[0]
print(f"Quotes with bid_status='未中标': {total_lost}")

# Count total quotes that would appear in heatmap (have project, positive price, NOT 未中标)
heatmap_eligible = conn.execute("""
    SELECT COUNT(*) FROM quotes q
    JOIN materials m ON q.material_id = m.id
    WHERE q.project_id IS NOT NULL
      AND q.unit_price > 0
      AND q.bid_status != '未中标'
""").fetchone()[0]
print(f"Heatmap-eligible quotes (excluding 未中标): {heatmap_eligible}")

heatmap_all = conn.execute("""
    SELECT COUNT(*) FROM quotes q
    JOIN materials m ON q.material_id = m.id
    WHERE q.project_id IS NOT NULL
      AND q.unit_price > 0
""").fetchone()[0]
print(f"Heatmap-eligible quotes (including 未中标): {heatmap_all}")
print(f"Difference: {heatmap_all - heatmap_eligible} (should match lost bids with project)")

# Verify those 未中标 quotes still participate in baseline computation
# (refresh_material_baselines does NOT exclude bid_status='未中标')
lost_with_price = conn.execute("""
    SELECT COUNT(*) FROM quotes WHERE bid_status = '未中标' AND unit_price > 0
""").fetchone()[0]
print(f"\n未中标 quotes with valid prices (still in baseline): {lost_with_price}")

conn.close()
print("\nDashboard exclusion verification complete.")
