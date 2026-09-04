"""Import historical procurement price data from versioned raw assets.

Governance:
  - Dry-run by default; --commit required to write.
  - --wipe backs up the DB then deletes all Quote + Material rows (and orphan
    Supplier records with no bid_submissions reference) inside one transaction.
  - Every imported Quote carries batch_id + extraction_meta_json (source_id,
    data_version, fact_status, source_row, brand_evidence_level).
  - Historical quotes have NO supplier_id — brand is stored in Quote.brand only.
    Supplier records are only created by the bid-comparison flow.
  - Conservation check: rows written == len(valid import records).

Usage:
    python scripts/import_historical.py                      # dry-run, latest raw/
    python scripts/import_historical.py --commit
    python scripts/import_historical.py --wipe --commit
    python scripts/import_historical.py --raw-dir docs/data/raw/2026-06-23 --commit
    python scripts/import_historical.py --batch-id hist-v2-2026-06-23 --commit
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RAW_BASE = ROOT / "docs" / "data" / "raw"
DB_PATH  = ROOT / "data" / "mempas.db"

# Strings in brand columns that are NOT brand names
_NON_BRAND = frozenset({
    "新增", "利用原有箱体", "新增配电箱", "新作", "见图", "详见",
    "甲供", "暂定", "-", "—", "nan", "None", "",
    # Column-label strings that appear in 配电箱 header rows
    "名称：", "名称:", "型号：", "型号:", "柜号：", "柜号:", "备注", "序号",
    "单价", "数量", "单位", "规格型号", "合计",
})

# Prefixes used as row-label text inside 配电箱 box-header cells
_PDB_LABEL_PREFIXES = ("柜号：", "型号：", "名称：", "柜号:", "型号:", "名称:")


def _strip_pdb_label(s: str) -> str:
    """Remove label prefixes like '柜号：' from a 配电箱 cell value."""
    for prefix in _PDB_LABEL_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s.strip()


def _is_brand_name(val: str) -> bool:
    s = val.strip()
    if not s or s in _NON_BRAND:
        return False
    if s[0].isdigit():  # dates ("2025.6.13…"), power ratings ("12KW"), dimensions
        return False
    # Reference codes like "T01_FB1-JG01" — all-ASCII with underscore
    if "_" in s and all(ord(c) < 128 for c in s):
        return False
    try:
        float(s)
        return False
    except ValueError:
        return len(s) >= 2


def _to_float(s) -> float | None:
    if pd.isna(s):
        return None
    s = str(s).strip().replace(",", ".").replace("，", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        v = float(s)
        return v if not np.isnan(v) else None
    except ValueError:
        return None


# ── raw directory helpers ──────────────────────────────────────────────────

def find_latest_raw_dir() -> Path | None:
    if not RAW_BASE.exists():
        return None
    for d in sorted(RAW_BASE.iterdir(), reverse=True):
        if d.is_dir() and (d / "manifest.json").exists():
            return d
    return None


def load_manifest(raw_dir: Path) -> dict:
    with open(raw_dir / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def _find_summary_csv(raw_dir: Path, src_entry: dict) -> Path | None:
    """Return the summary sheet CSV, preferring 汇总 over Sheet1."""
    csv_dir = raw_dir / src_entry["csv_dir"]
    sheets_by_name = {sh["sheet"]: sh for sh in src_entry["sheets"]}
    # Prefer explicit summary sheet names in priority order
    for preferred in ("汇总", "Sheet1"):
        if preferred in sheets_by_name:
            return csv_dir / sheets_by_name[preferred]["csv"]
    # Fallback: largest sheet
    best = max(src_entry["sheets"], key=lambda e: e["rows"], default=None)
    return (csv_dir / best["csv"]) if best else None


# ── per-category parsers (delegate to analyze_data.py) ────────────────────

def _parse_category(category: str, csv_path: Path) -> pd.DataFrame:
    from scripts.analyze_data import (
        parse_不锈钢管,
        parse_桥架,
        parse_水箱,
        parse_潜水泵,
        parse_空调泵,
        parse_阀门,
        parse_风口风阀,
        parse_风机盘管,
    )
    from scripts.analyze_data import (
        parse_母线 as parse_母线槽,
    )
    parsers = {
        "桥架":    parse_桥架,
        "阀门":    parse_阀门,
        "风口风阀": parse_风口风阀,
        "母线槽":  parse_母线槽,
        "不锈钢管": parse_不锈钢管,
        "水箱":    parse_水箱,
        "潜水泵":  parse_潜水泵,
        "风机盘管": parse_风机盘管,
        "空调泵":  parse_空调泵,
    }
    fn = parsers.get(category)
    if fn is None:
        raise ValueError(f"No parser for category: {category}")
    return fn(csv_path)


def _df_to_records(
    df: pd.DataFrame,
    category: str,
    profession: str,
    src_entry: dict,
    manifest_date: str,
    batch_id: str,
) -> tuple[list[dict], int]:
    """Convert a parsed DataFrame into import records. Returns (records, skipped_count)."""
    from scripts.analyze_data import classify_subcat

    source_id    = src_entry["source_id"]
    data_version = f"raw-{manifest_date}"
    records: list[dict] = []
    skipped = 0

    for idx, row in df.iterrows():
        price = _to_float(row.get("单价"))
        if price is None or price <= 0:
            skipped += 1
            continue

        name = str(row.get("名称", "")).strip() if pd.notna(row.get("名称")) else ""
        if not name or name == category:
            skipped += 1
            continue

        spec = ""
        for col in ("规格", "规格型号", "型号"):
            if col in row.index and pd.notna(row[col]):
                spec = re.sub(r"\s+", " ", str(row[col])).strip()
                break

        unit_raw = str(row.get("单位", "")).strip() if pd.notna(row.get("单位")) else ""
        if unit_raw in ("0", "nan", "None"):
            unit_raw = ""
        unit = unit_raw or _DEFAULT_UNIT.get(category, "")

        brand_raw = ""
        if "品牌" in row.index and pd.notna(row["品牌"]):
            b = str(row["品牌"]).strip()
            if _is_brand_name(b):
                brand_raw = b

        price_excl = None
        for col in ("单价_不含税", "单价不含税", "不含税单价"):
            if col in row.index:
                v = _to_float(row[col])
                if v and v > 0:
                    price_excl = v
                    break

        quantity = None
        for col in ("数量", "数量_n", "工程量"):
            if col in row.index:
                v = _to_float(row[col])
                if v and v > 0:
                    quantity = v
                    break

        remark = str(row.get("备注", "")).strip() if pd.notna(row.get("备注")) else ""

        records.append({
            "name":               name,
            "spec":               spec,
            "unit":               unit,
            "brand_raw":          brand_raw,
            "category":           category,
            "profession":         profession,
            "sub_category":       classify_subcat(name, category),
            "project_name":       "",
            "unit_price":         float(price),
            "unit_price_excl_tax": float(price_excl) if price_excl else None,
            "quantity":           float(quantity) if quantity else None,
            "remark":             remark,
            "batch_id":           batch_id,
            "meta": {
                "source_id":            source_id,
                "data_version":         data_version,
                "fact_status":          "approved",
                "source_row":           int(idx),
                "brand_evidence_level": "quoted",
            },
        })

    return records, skipped


def _pdb_records(
    raw_dir: Path,
    manifest: dict,
    batch_id: str,
) -> list[dict]:
    """Extract box-level price records from 配电箱 project sheets."""
    from scripts.analyze_data import classify_subcat

    src_entry = next(
        (s for s in manifest["sources"] if s["category"] == "配电箱"), None
    )
    if not src_entry:
        return []

    csv_dir      = raw_dir / src_entry["csv_dir"]
    source_id    = src_entry["source_id"]
    data_version = f"raw-{manifest.get('generated_at', 'unknown')}"
    records: list[dict] = []

    for sh in src_entry["sheets"]:
        project = sh["sheet"]
        if project == "汇总":
            continue
        csv_path = csv_dir / sh["csv"]
        if not csv_path.exists():
            continue

        df = pd.read_csv(csv_path, header=None, dtype=str, encoding="utf-8-sig")
        current_box: str | None = None
        brand_raw = ""

        for idx, row in df.iterrows():
            vals = [str(v).strip() if pd.notna(v) else "" for v in row]
            if not vals:
                continue

            # Box header: first cell matches "digit-digit" pattern
            if re.match(r"^\d+-\d+", vals[0]):
                # Strip label prefixes ("柜号：", "型号：") from name cells
                clean_parts = [_strip_pdb_label(v) for v in vals[1:3]]
                name_parts = [v for v in clean_parts if v]
                current_box = " ".join(name_parts) if name_parts else vals[0]
                brand_raw = next(
                    (v for v in vals[3:] if _is_brand_name(v)), ""
                )
                continue

            # Box total row — use only "单台合计" (per-unit price).
            # "总计" rows have a quantity column before the price which causes
            # the first-positive-number search to return the quantity (often 1).
            if current_box and any("单台合计" in v for v in vals[:3]):
                price = next(
                    (p for v in vals[3:] if (p := _to_float(v)) and p > 1), None
                )
                if price:
                    records.append({
                        "name":               current_box,
                        "spec":               "",
                        "unit":               "台",
                        "brand_raw":          brand_raw,
                        "category":           "配电箱",
                        "profession":         "电气",
                        "sub_category":       classify_subcat(current_box, "配电箱"),
                        "project_name":       project,
                        "unit_price":         float(price),
                        "unit_price_excl_tax": None,
                        "quantity":           None,
                        "remark":             "",
                        "batch_id":           batch_id,
                        "meta": {
                            "source_id":            source_id,
                            "data_version":         data_version,
                            "fact_status":          "approved",
                            "source_row":           int(idx),
                            "brand_evidence_level": "quoted",
                        },
                    })

    return records


def build_all_records(raw_dir: Path, manifest: dict, batch_id: str) -> list[dict]:
    manifest_date = manifest.get("generated_at", "unknown")
    all_records: list[dict] = []

    for src_entry in manifest["sources"]:
        category  = src_entry["category"]
        profession = src_entry["profession"]
        if category == "配电箱":
            continue

        csv_path = _find_summary_csv(raw_dir, src_entry)
        if not csv_path or not csv_path.exists():
            print(f"  [SKIP] {src_entry['source_id']}: no summary CSV")
            continue

        try:
            df = _parse_category(category, csv_path)
        except Exception as exc:
            print(f"  [ERROR] {src_entry['source_id']}: {exc}", file=sys.stderr)
            continue

        recs, skipped = _df_to_records(
            df, category, profession, src_entry, manifest_date, batch_id
        )
        print(f"  [{category:8s}] {len(df):4d} parsed  "
              f"→ {len(recs):4d} valid  (skipped {skipped})")
        all_records.extend(recs)

    pdb = _pdb_records(raw_dir, manifest, batch_id)
    print(f"  [配电箱  ] {len(pdb):4d} box-level records")
    all_records.extend(pdb)

    return all_records


# ── database operations ────────────────────────────────────────────────────

# Default units for categories whose Excel templates have no 单位 column
_DEFAULT_UNIT: dict[str, str] = {
    "潜水泵": "台",
    "空调泵": "台",
}

_PROF_ABBR = {"电气": "EL", "给排水": "WP", "暖通": "HV"}
_CAT_ABBR  = {
    "桥架": "TRY", "母线槽": "BUS", "配电箱": "PDB", "阀门": "VLV",
    "不锈钢管": "SSP", "水箱": "WTK", "潜水泵": "SBP",
    "风口风阀": "AVC", "风机盘管": "FCU", "空调泵": "ACP",
}


def _mat_code(profession: str, category: str, seq: int) -> str:
    return f"{_PROF_ABBR.get(profession, 'OT')}-{_CAT_ABBR.get(category, 'OTH')}-{seq:05d}"


def backup_db():
    if not DB_PATH.exists():
        print("[BACKUP] DB not found — skipping backup")
        return
    bak = DB_PATH.with_suffix(f".{date.today().strftime('%Y%m%d')}.bak.db")
    shutil.copy2(DB_PATH, bak)
    print(f"[BACKUP] {DB_PATH.name} → {bak.name}")


def wipe_historical_data():
    """Delete all Quote + Material + orphan Project rows inside one transaction.

    Order:
      1. NULL out bid_quote_lines.material_id (FK reference to materials, nullable)
      2. DELETE FROM quotes
      3. DELETE FROM materials
      4. DELETE orphan projects (not referenced by bid_submissions)
    """
    from sqlalchemy import text

    from apps.api.core.database import SessionLocal

    db = SessionLocal()
    try:
        q_before = db.execute(text("SELECT COUNT(*) FROM quotes")).scalar()
        m_before = db.execute(text("SELECT COUNT(*) FROM materials")).scalar()
        p_before = db.execute(text("SELECT COUNT(*) FROM projects")).scalar()
        print(f"[WIPE] Before: {q_before} quotes, {m_before} materials, {p_before} projects")

        # Release FK references from bid_quote_lines to materials
        db.execute(text(
            "UPDATE bid_quote_lines SET material_id = NULL WHERE material_id IS NOT NULL"
        ))
        # Release FK references from bid_alignment_items to quotes (quote_id → quotes.id)
        db.execute(text(
            "UPDATE bid_alignment_items SET quote_id = NULL WHERE quote_id IS NOT NULL"
        ))
        db.execute(text("DELETE FROM quotes"))
        db.execute(text("DELETE FROM materials"))
        # Suppliers: do NOT delete — bid_alignment_items.supplier_id and
        # bid_invitations.supplier_id reference real supplier records.
        # Orphan brand-as-supplier records are harmless: they have no quotes
        # and valid_quote_filters handles supplier_id IS NULL correctly.
        db.commit()

        q_after = db.execute(text("SELECT COUNT(*) FROM quotes")).scalar()
        m_after = db.execute(text("SELECT COUNT(*) FROM materials")).scalar()
        print(f"[WIPE] After:  {q_after} quotes, {m_after} materials "
              f"(suppliers/projects unchanged)")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def write_records(records: list[dict]) -> tuple[int, int]:
    """Write all records inside one transaction. Returns (quotes_written, materials_created).

    Material deduplication: find-or-create by (category, standard_name, spec, unit).
    Each source row creates exactly one Quote; multiple rows with the same spec
    share one Material record — enabling multi-point price baselines.
    """
    from apps.api.core.database import SessionLocal
    from apps.api.models import Material, Project, Quote
    from apps.api.services.history.statistics import refresh_material_baselines

    db = SessionLocal()
    # (category, name, spec, unit) → material_id
    mat_cache:      dict[tuple, int] = {}
    project_cache:  dict[str, int] = {}
    cat_seq:        dict[str, int] = {}
    written = 0
    mats_created = 0

    try:
        for rec in records:
            category   = rec["category"]
            profession = rec["profession"]
            name       = rec["name"]
            spec       = rec.get("spec", "") or ""
            unit       = rec.get("unit", "") or ""

            mat_key = (category, name, spec, unit)
            if mat_key not in mat_cache:
                # New unique material — create it
                seq  = cat_seq.get(category, 1)
                code = _mat_code(profession, category, seq)
                cat_seq[category] = seq + 1

                mat = Material(
                    material_code=code,
                    standard_name=name,
                    profession=profession,
                    category=category,
                    sub_category=rec["sub_category"],
                    spec=spec,
                    unit=unit,
                )
                db.add(mat)
                db.flush()
                mat_cache[mat_key] = mat.id
                mats_created += 1

            mat_id = mat_cache[mat_key]

            # Project (配电箱 only)
            project_id: int | None = None
            proj_name = rec.get("project_name", "")
            if proj_name:
                if proj_name not in project_cache:
                    existing = db.query(Project).filter(Project.name == proj_name).first()
                    if not existing:
                        existing = Project(name=proj_name)
                        db.add(existing)
                        db.flush()
                    project_cache[proj_name] = existing.id
                project_id = project_cache[proj_name]

            quote = Quote(
                material_id=mat_id,
                supplier_id=None,
                project_id=project_id,
                unit_price=rec["unit_price"],
                unit_price_excl_tax=rec.get("unit_price_excl_tax"),
                quantity=rec.get("quantity"),
                brand=rec.get("brand_raw", ""),
                remark=rec.get("remark", ""),
                batch_id=rec["batch_id"],
                extraction_meta_json=rec["meta"],
            )
            db.add(quote)
            written += 1

        # Conservation check: every source row must produce exactly one Quote
        if written != len(records):
            raise RuntimeError(
                f"Conservation check failed: expected {len(records)}, wrote {written}"
            )

        db.commit()
        print(f"[COMMIT] {written} quotes, {mats_created} unique materials")

        # Refresh price baselines in a fresh session
        db2 = SessionLocal()
        try:
            print("[REFRESH] Recomputing price baselines...")
            refresh_material_baselines(db2)
            db2.commit()
            print("[REFRESH] Done")
        finally:
            db2.close()

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return written, mats_created


# ── dry-run report ─────────────────────────────────────────────────────────

def print_dry_run_report(records: list[dict]):
    cat_counts  = Counter(r["category"] for r in records)
    has_brand   = sum(1 for r in records if r.get("brand_raw"))
    no_brand    = len(records) - has_brand

    print("\n──────────────────────────────")
    print("DRY RUN REPORT (nothing written)")
    print("──────────────────────────────")
    print(f"  {'Category':12s}  {'Records':>7s}")
    print(f"  {'-'*12}  {'-'*7}")
    for cat, cnt in sorted(cat_counts.items()):
        print(f"  {cat:12s}  {cnt:>7d}")
    print(f"  {'─'*12}  {'─'*7}")
    print(f"  {'Total':12s}  {len(records):>7d}")
    print()
    print(f"  With brand field : {has_brand}")
    print(f"  No brand         : {no_brand}")
    print("  supplier_id      : None (all — brand stored in Quote.brand only)")
    print()
    print("Run with --commit to write.  Add --wipe to clear existing data first.")


# ── main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir",  default=None,
                    help="Versioned raw directory (auto-detect latest if omitted)")
    ap.add_argument("--batch-id", default=None,
                    help="Batch tag (default: hist-v1-<date>)")
    ap.add_argument("--commit",   action="store_true",
                    help="Write to database (dry-run otherwise)")
    ap.add_argument("--wipe",     action="store_true",
                    help="Delete existing Quote+Material records before import (requires --commit)")
    args = ap.parse_args()

    if args.wipe and not args.commit:
        print("[ERROR] --wipe requires --commit", file=sys.stderr)
        sys.exit(1)

    raw_dir = Path(args.raw_dir) if args.raw_dir else find_latest_raw_dir()
    if not raw_dir or not raw_dir.exists():
        print("[ERROR] No raw assets directory found. Run build_raw_assets.py first.",
              file=sys.stderr)
        sys.exit(1)

    batch_id = args.batch_id or f"hist-v1-{date.today()}"
    mode     = "COMMIT" if args.commit else "DRY RUN"

    print(f"[CONFIG] raw_dir  = {raw_dir}")
    print(f"[CONFIG] batch_id = {batch_id}")
    print(f"[CONFIG] mode     = {mode}")
    print()

    manifest = load_manifest(raw_dir)
    print(f"[PARSE] Building import records from {raw_dir.name} …")
    records = build_all_records(raw_dir, manifest, batch_id)
    print(f"\n[TOTAL] {len(records)} importable records")

    if not args.commit:
        print_dry_run_report(records)
        return

    if args.wipe:
        backup_db()
        wipe_historical_data()

    written, mats_created = write_records(records)
    print(f"\nImport complete — batch: {batch_id}, {written} quotes, {mats_created} materials")


if __name__ == "__main__":
    main()
