"""Preview 配电箱 box-level records from _pdb_records() directly."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.import_historical import _pdb_records, find_latest_raw_dir, load_manifest

raw_dir = find_latest_raw_dir()
manifest = load_manifest(raw_dir)
recs = _pdb_records(raw_dir, manifest, "preview")

price1 = [r for r in recs if r["unit_price"] <= 1]
bad_names = [r for r in recs if any(x in r["name"] for x in ["柜号", "型号", "名称"])]
bad_brands = [r for r in recs if any(x in r.get("brand_raw","") for x in ["名称", "型号", "序号"])]

print(f"Total PDB records: {len(recs)}")
print(f"price <= 1: {len(price1)}")
print(f"bad names (含柜号/型号): {len(bad_names)}")
print(f"bad brands: {len(bad_brands)}")

print("\n=== 各项目记录数 ===")
from collections import Counter
c = Counter(r["project_name"] for r in recs)
for proj, n in sorted(c.items()):
    print(f"  {proj:20s}  n={n}")

print("\n=== 样本（前10条）===")
for r in recs[:10]:
    print(f"  [{r['project_name'][:12]:12s}] {r['name'][:30]:30s}  brand={r['brand_raw'][:15]:15s}  price={r['unit_price']}")

if price1:
    print("\n=== 仍有 price<=1 的样本 ===")
    for r in price1[:5]:
        print(f"  {r['name'][:30]}  price={r['unit_price']}")

if bad_names:
    print("\n=== 仍有前缀的名称 ===")
    for r in bad_names[:5]:
        print(f"  {r['name']}")
