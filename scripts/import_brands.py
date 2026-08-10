"""Import approved brand registry from Excel into brand_tiers table.

Usage:
    python scripts/import_brands.py                  # dry-run
    python scripts/import_brands.py --commit         # write to DB
    python scripts/import_brands.py --wipe --commit  # clear existing + write
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BRAND_FILE = Path("docs/项目资料/用户反馈/2026-06-23/品牌.xlsx")

# Brand file category → system category
CATEGORY_MAP = {
    "配电箱":       "配电箱",
    "电力电缆":     "电力电缆",
    "桥架":         "桥架",
    "母线槽":       "母线槽",
    "生活水泵":     "空调泵",
    "排水泵":       "排水泵",
    "阀门":         "阀门",
    "不锈钢生活水箱": "水箱",
    "不锈钢管":     "不锈钢管",
    "风口风阀/消声器": "风口风阀",
    "油烟净化器":   "油烟净化器",
}

# Known aliases: (raw_name_in_file, canonical_name)
# Add more as discovered; comparisons are case-insensitive strip
ALIASES: list[tuple[str, str]] = [
    ("kitz",   "KITZ"),
    ("开兹",   "KITZ"),
    ("watts",  "WATTS"),
    ("沃茨",   "WATTS"),
    ("abb",    "ABB"),
    ("crane美国克瑞", "CRANE"),
    ("taikefei泰科菲", "泰科菲"),
    ("watts、圣戈班pam", "WATTS"),
]

def _canonical(name: str) -> str:
    key = name.strip().lower()
    for raw, canon in ALIASES:
        if key == raw.lower():
            return canon
    return name.strip()


def load_records() -> list[dict]:
    df = pd.read_excel(BRAND_FILE, sheet_name="Sheet1", header=None)
    df.columns = ["seq", "category_raw", "brand_raw", "origin_raw"]
    df["seq"] = df["seq"].ffill()
    df["category_raw"] = df["category_raw"].ffill()
    df = df.dropna(subset=["brand_raw"])

    # Drop header row
    df = df[df["brand_raw"] != "品牌"]

    records = []
    for _, row in df.iterrows():
        cat_raw = str(row["category_raw"]).strip()
        brand_raw = str(row["brand_raw"]).strip()
        origin_raw = str(row["origin_raw"]).strip() if pd.notna(row["origin_raw"]) else "国产"

        category = CATEGORY_MAP.get(cat_raw, cat_raw)
        tier = "合资" if "合资" in origin_raw else "国产"
        canon = _canonical(brand_raw)
        alias_of = canon if canon != brand_raw.strip() else None

        records.append({
            "brand_name":     brand_raw,
            "tier":           tier,
            "category":       category,
            "is_approved":    True,
            "canonical_name": canon,
            "alias_of":       alias_of,
        })
    return records


def wipe(db) -> None:
    from sqlalchemy import text
    n = db.execute(text("SELECT COUNT(*) FROM brand_tiers WHERE is_approved = 1")).scalar()
    db.execute(text("DELETE FROM brand_tiers WHERE is_approved = 1"))
    db.commit()
    print(f"[WIPE] Removed {n} approved brand records")


def write_records(records: list[dict]) -> int:
    from apps.api.core.database import SessionLocal
    from apps.api.models.brand_tier import BrandTier

    db = SessionLocal()
    written = 0
    try:
        for r in records:
            db.add(BrandTier(**r))
            written += 1
        if written != len(records):
            raise RuntimeError(f"Conservation check failed: {written} != {len(records)}")
        db.commit()
        print(f"[COMMIT] {written} brand records written")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return written


def print_report(records: list[dict]) -> None:
    from collections import Counter
    cat_counts = Counter(r["category"] for r in records)
    tier_counts = Counter(r["tier"] for r in records)
    alias_count = sum(1 for r in records if r["alias_of"])

    print("\n──────────────────────────────")
    print("DRY RUN REPORT (nothing written)")
    print("──────────────────────────────")
    print(f"  {'品类':16s}  {'品牌数':>5s}")
    print(f"  {'-'*16}  {'-'*5}")
    for cat, cnt in sorted(cat_counts.items()):
        print(f"  {cat:16s}  {cnt:>5d}")
    print(f"  {'─'*16}  {'─'*5}")
    print(f"  {'合计':16s}  {len(records):>5d}")
    print()
    print(f"  合资: {tier_counts.get('合资', 0)}  国产: {tier_counts.get('国产', 0)}")
    print(f"  别名记录: {alias_count}")
    print()
    print("Run with --commit to write. Add --wipe to clear existing first.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--wipe", action="store_true")
    args = parser.parse_args()

    records = load_records()

    if not args.commit:
        print_report(records)
        return

    if args.wipe:
        from apps.api.core.database import SessionLocal
        db = SessionLocal()
        try:
            wipe(db)
        finally:
            db.close()

    write_records(records)
    print(f"\nImport complete — {len(records)} approved brands")


if __name__ == "__main__":
    main()
