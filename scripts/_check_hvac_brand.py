"""Check that HVAC pump brand noise (*KW) is now correctly filtered."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.import_historical import (
    find_latest_raw_dir, load_manifest, _parse_category, _find_summary_csv, _df_to_records
)

raw_dir = find_latest_raw_dir()
manifest = load_manifest(raw_dir)

for src in manifest["sources"]:
    if src["category"] != "空调泵":
        continue
    csv_path = _find_summary_csv(raw_dir, src)
    df = _parse_category("空调泵", csv_path)
    recs, skipped = _df_to_records(df, "空调泵", "暖通", src, manifest["generated_at"], "preview")
    brands = [r["brand_raw"] for r in recs if r.get("brand_raw")]
    kw_brands = [b for b in brands if b[0].isdigit()]
    print(f"空调泵: {len(recs)} records, {len(brands)} with brand, {len(kw_brands)} *KW noise")
    if kw_brands:
        print(f"  KW brands still present: {kw_brands[:5]}")
    else:
        print(f"  Sample brands: {list(set(brands))[:8]}")
