"""Import existing CSV data (from docs/data/) into the SQLite database.

Reuses parsers from analyze_data.py to load all 10 categories.
Run: python -m scripts.import_data
"""

import json
import sys
from pathlib import Path

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from server.database import get_engine, Base, SessionLocal
from server.models import (
    Material, Supplier, Project, Quote, AnalysisConfig,
    PROFESSION_MAP, PROFESSION_ABBR, CATEGORY_ABBR,
    DEFAULT_SCORING_WEIGHTS, DEFAULT_THRESHOLDS,
)
from scripts.analyze_data import (
    parse_桥架, parse_阀门, parse_风口风阀, parse_母线 as parse_母线槽,
    parse_不锈钢管, parse_水箱, parse_潜水泵, parse_风机盘管, parse_空调泵,
    parse_配电箱_all, classify_subcat, _to_float,
)

DATA_DIR = PROJECT_ROOT / "docs" / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"

# Category → parser function (uses summary sheet)
CATEGORY_PARSERS = {
    "桥架": parse_桥架,
    "阀门": parse_阀门,
    "风口风阀": parse_风口风阀,
    "母线槽": parse_母线槽,
    "不锈钢管": parse_不锈钢管,
    "水箱": parse_水箱,
    "潜水泵": parse_潜水泵,
    "风机盘管": parse_风机盘管,
    "空调泵": parse_空调泵,
}


def load_manifest() -> list[dict]:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _gen_code(profession: str, category: str, seq: int) -> str:
    prof_abbr = PROFESSION_ABBR.get(profession, "OT")
    cat_abbr = CATEGORY_ABBR.get(category, "OTH")
    return f"{prof_abbr}-{cat_abbr}-{seq:05d}"


def import_all():
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        manifest = load_manifest()

        # ── Init default configs ─────────────────────────────────────────
        for key, value, desc in [
            ("scoring_weights", DEFAULT_SCORING_WEIGHTS, "供应商评分权重"),
            ("thresholds", DEFAULT_THRESHOLDS, "各品类价格偏差容差"),
        ]:
            if not db.query(AnalysisConfig).filter(AnalysisConfig.key == key).first():
                db.add(AnalysisConfig(key=key, value=value, description=desc))
        db.commit()

        # ── Collect project names from manifest ──────────────────────────
        project_names = set()
        for entry in manifest:
            sheet = entry["sheet"]
            if sheet in ("汇总", "Sheet1") or any(
                k in sheet for k in ("计价", "参数", "明细", "投标", "技术")
            ):
                continue
            project_names.add(sheet)

        project_map = {}
        for name in sorted(project_names):
            proj = Project(name=name)
            db.add(proj)
            db.flush()
            project_map[name] = proj.id
        db.commit()
        print(f"Imported {len(project_map)} projects")

        # ── Parse each category ──────────────────────────────────────────
        total_materials = 0
        total_quotes = 0
        supplier_names = set()

        for category, parser in CATEGORY_PARSERS.items():
            profession = PROFESSION_MAP[category]
            entries = [e for e in manifest if e["category"] == category]

            # Find summary CSV
            summary_entry = None
            for e in entries:
                if e["sheet"] == "汇总":
                    summary_entry = e
                    break
            if not summary_entry:
                for e in entries:
                    if e["rows"] == max(x["rows"] for x in entries):
                        summary_entry = e
                        break

            if not summary_entry:
                print(f"  [{category}] No summary CSV found, skipping")
                continue

            csv_path = DATA_DIR / summary_entry["csv"]
            df = parser(csv_path)
            print(f"  [{category}] Parsed {len(df)} rows from {summary_entry['csv']}")

            # Classify sub-categories
            name_col = "名称" if "名称" in df.columns else None
            if name_col:
                df["子类"] = df[name_col].apply(lambda x: classify_subcat(x, category))
            else:
                df["子类"] = "未分类"

            # Create materials and quotes
            seq = 1
            for _, row in df.iterrows():
                name = str(row.get("名称", "")).strip() if pd.notna(row.get("名称")) else category
                if not name:
                    name = category

                spec = ""
                for col in ("规格", "规格型号", "型号"):
                    if col in row.index and pd.notna(row[col]):
                        spec = str(row[col]).strip()
                        break

                brand = ""
                if "品牌" in row.index and pd.notna(row["品牌"]):
                    b = str(row["品牌"]).strip()
                    try:
                        float(b)
                    except ValueError:
                        if b and b != "nan":
                            brand = b
                            supplier_names.add(brand)

                material_type = ""
                for col in ("材质", "材质牌号", "阀体材质"):
                    if col in row.index and pd.notna(row[col]):
                        material_type = str(row[col]).strip()
                        break

                unit = ""
                if "单位" in row.index and pd.notna(row["单位"]):
                    unit = str(row["单位"]).strip()

                sub_cat = str(row.get("子类", "")).strip() or "未分类"

                code = _gen_code(profession, category, seq)
                seq += 1

                mat = Material(
                    material_code=code,
                    standard_name=name,
                    profession=profession,
                    category=category,
                    sub_category=sub_cat,
                    spec=spec,
                    material_type=material_type,
                    unit=unit,
                    brand=brand,
                )
                db.add(mat)
                db.flush()
                total_materials += 1

                # Create quote
                price = None
                if "单价" in row.index:
                    price = _to_float(row["单价"]) if isinstance(row["单价"], str) else row["单价"]
                    if pd.isna(price) or (price is not None and price <= 0):
                        price = None

                price_excl = None
                for col in ("单价不含税", "不含税单价", "单价_不含税"):
                    if col in row.index:
                        v = _to_float(row[col]) if isinstance(row[col], str) else row[col]
                        if pd.notna(v) and v > 0:
                            price_excl = float(v)
                            break

                quantity = None
                for col in ("数量", "数量_n", "工程量"):
                    if col in row.index:
                        v = _to_float(row[col]) if isinstance(row[col], str) else row[col]
                        if pd.notna(v) and v > 0:
                            quantity = float(v)
                            break

                remark = ""
                if "备注" in row.index and pd.notna(row["备注"]):
                    remark = str(row["备注"]).strip()

                quote = Quote(
                    material_id=mat.id,
                    unit_price=float(price) if price and not np.isnan(price) else None,
                    unit_price_excl_tax=price_excl,
                    quantity=quantity,
                    brand=brand,
                    remark=remark,
                )
                db.add(quote)
                total_quotes += 1

            db.commit()

        # ── Handle 配电箱 separately (BOM structure) ─────────────────────
        category = "配电箱"
        profession = PROFESSION_MAP[category]
        pdb_df = parse_配电箱_all(manifest)
        print(f"  [配电箱] Parsed {len(pdb_df)} box-level records")
        seq = 1
        for _, row in pdb_df.iterrows():
            name = str(row["名称"]).strip()
            code = _gen_code(profession, category, seq)
            seq += 1

            sub_cat = classify_subcat(name, category)

            mat = Material(
                material_code=code,
                standard_name=name,
                profession=profession,
                category=category,
                sub_category=sub_cat,
                spec="",
                unit="台",
            )
            db.add(mat)
            db.flush()
            total_materials += 1

            project_name = str(row.get("项目", "")).strip()
            project_id = project_map.get(project_name)

            quote = Quote(
                material_id=mat.id,
                project_id=project_id,
                unit_price=float(row["单价"]) if pd.notna(row["单价"]) else None,
                brand=str(row.get("品牌", "")).strip(),
            )
            db.add(quote)
            total_quotes += 1

        db.commit()

        # ── Create supplier records from collected brand names ───────────
        for brand_name in sorted(supplier_names):
            if not brand_name:
                continue
            existing = db.query(Supplier).filter(Supplier.name == brand_name).first()
            if not existing:
                db.add(Supplier(name=brand_name))
        db.commit()
        sup_count = db.query(Supplier).count()
        print(f"Imported {sup_count} suppliers (from brand names)")

        # ── Compute baselines ────────────────────────────────────────────
        print("Computing price baselines...")
        from server.services.analysis import refresh_material_baselines
        refresh_material_baselines(db)

        print(f"\nDone! {total_materials} materials, {total_quotes} quotes imported.")

    finally:
        db.close()


if __name__ == "__main__":
    import_all()
