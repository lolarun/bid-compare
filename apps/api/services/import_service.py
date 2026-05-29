"""Excel/CSV import service — v2.

Key fixes vs v1:
- Material dedup by (category, standardized_name, spec).
- Brand is NOT used as supplier; supplier column detected separately.
- standardize_name() applied before Material lookup/creation.
- brand_tier looked up in brand_tiers table; unknown brands returned.
- refresh_material_baselines called at end.
"""

import io
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from apps.api.models import Material, Supplier, Quote, Project, BrandTier
from apps.api.core.config import PROFESSION_MAP, PROFESSION_ABBR, CATEGORY_ABBR
from apps.api.services.standardize import standardize_name


def _to_float(s):
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return np.nan
    s = str(s).strip().replace(",", ".").replace("，", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return np.nan


def _gen_code(db: Session, profession: str, category: str) -> str:
    prof_abbr = PROFESSION_ABBR.get(profession, "OT")
    cat_abbr = CATEGORY_ABBR.get(category, "OTH")
    prefix = f"{prof_abbr}-{cat_abbr}-"
    last = db.query(Material).filter(
        Material.material_code.like(f"{prefix}%")
    ).order_by(Material.material_code.desc()).first()
    seq = int(last.material_code.split("-")[-1]) + 1 if last and last.material_code else 1
    return f"{prefix}{seq:05d}"


def _generate_batch_id() -> str:
    return f"IMP-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def _get_or_create_supplier(db: Session, name: str) -> Supplier | None:
    if not name or not name.strip():
        return None
    name = name.strip()
    sup = db.query(Supplier).filter(Supplier.name == name).first()
    if not sup:
        sup = Supplier(name=name)
        db.add(sup)
        db.flush()
    return sup


def _get_or_create_project(db: Session, name: str) -> Project | None:
    if not name or not name.strip():
        return None
    name = name.strip()
    proj = db.query(Project).filter(Project.name == name).first()
    if not proj:
        proj = Project(name=name)
        db.add(proj)
        db.flush()
    return proj


def _get_or_create_material(
    db: Session,
    standard_name: str,
    category: str,
    profession: str,
    spec: str = "",
    material_type: str = "",
    unit: str = "",
    brand: str = "",
) -> Material:
    """Find existing material by (category, standard_name, spec) or create new."""
    q = db.query(Material).filter(
        Material.category == category,
        Material.standard_name == standard_name,
    )
    if spec:
        q = q.filter(Material.spec == spec)
    mat = q.first()
    if mat:
        return mat

    # Create new — material_code optional (v2: 编号留白为主，这里自动生成备用)
    code = _gen_code(db, profession, category)
    mat = Material(
        material_code=code,
        standard_name=standard_name,
        profession=profession,
        category=category,
        spec=spec,
        material_type=material_type,
        unit=unit,
        brand=brand,
    )
    db.add(mat)
    db.flush()
    return mat


def _lookup_brand_tier(db: Session, brand: str, category: str) -> str:
    """Look up brand tier; return empty string if unknown."""
    if not brand:
        return ""
    # Category-specific first, then generic
    bt = db.query(BrandTier).filter(
        BrandTier.brand_name == brand,
        BrandTier.category == category,
    ).first()
    if bt:
        return bt.tier
    bt = db.query(BrandTier).filter(
        BrandTier.brand_name == brand,
        BrandTier.category.is_(None),
    ).first()
    return bt.tier if bt else ""


def import_csv_data(
    db: Session,
    file_content: bytes,
    filename: str,
    category: str,
    project_name: str = "",
    default_supplier_id: int | None = None,
    bid_status: str = "",
) -> dict:
    """Import a CSV or Excel file for a given category.

    Returns: {status, batch_id, imported, skipped, errors, unknown_brands}
    """
    profession = PROFESSION_MAP.get(category)
    if not profession:
        return {
            "status": "error", "batch_id": "", "imported": 0, "skipped": 0,
            "errors": [{"row": 0, "reason": f"Unknown category: {category}"}],
            "unknown_brands": [],
        }

    batch_id = _generate_batch_id()
    errors: list[dict] = []
    imported = 0
    skipped = 0
    unknown_brands: set[str] = set()
    supplier_ids: set[int] = set()

    try:
        if filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(file_content), header=0, dtype=str)
        else:
            df = pd.read_csv(io.BytesIO(file_content), header=0, dtype=str, encoding="utf-8-sig")
    except Exception as e:
        return {
            "status": "error", "batch_id": batch_id, "imported": 0, "skipped": 0,
            "errors": [{"row": 0, "reason": f"File parse error: {e}"}],
            "unknown_brands": [],
        }

    project = _get_or_create_project(db, project_name) if project_name else None
    col_map = _detect_columns(df.columns.tolist())

    for idx, row in df.iterrows():
        row_num = int(idx) + 2  # type: ignore[arg-type]
        try:
            raw_name = _get_col_value(row, col_map, "name")
            if not raw_name:
                skipped += 1
                continue

            # 标准化名称
            std_result = standardize_name(raw_name, category)
            standard_name = std_result["standardized"]

            spec = _get_col_value(row, col_map, "spec") or ""
            brand = _get_col_value(row, col_map, "brand") or ""
            supplier_name = _get_col_value(row, col_map, "supplier") or ""
            material_type = _get_col_value(row, col_map, "material_type") or ""
            unit = _get_col_value(row, col_map, "unit") or ""
            remark = _get_col_value(row, col_map, "remark") or ""
            sub_category = _get_col_value(row, col_map, "sub_category") or ""

            price_str = _get_col_value(row, col_map, "price")
            price = _to_float(price_str) if price_str else None
            if price is not None and (np.isnan(price) or price <= 0):
                price = None

            price_excl_str = _get_col_value(row, col_map, "price_excl")
            price_excl = _to_float(price_excl_str) if price_excl_str else None
            if price_excl is not None and (np.isnan(price_excl) or price_excl <= 0):
                price_excl = None

            qty_str = _get_col_value(row, col_map, "quantity")
            qty = _to_float(qty_str) if qty_str else None
            if qty is not None and (np.isnan(qty) or qty <= 0):
                qty = None

            # 计算总价
            total_price = None
            if price is not None and qty is not None:
                total_price = round(price * qty, 4)

            # 找或建 Material（去重）
            mat = _get_or_create_material(
                db, standard_name, category, profession,
                spec=spec, material_type=material_type, unit=unit, brand=brand,
            )
            if sub_category and not mat.sub_category:
                mat.sub_category = sub_category

            # 供应商（从专用列，不用品牌；fallback to default_supplier_id）
            supplier = _get_or_create_supplier(db, supplier_name) if supplier_name else None
            if not supplier and default_supplier_id:
                supplier = db.get(Supplier, default_supplier_id)
            if supplier:
                supplier_ids.add(supplier.id)

            # 品牌档位
            brand_tier = ""
            if brand:
                brand_tier = _lookup_brand_tier(db, brand, category)
                if not brand_tier:
                    unknown_brands.add(brand)

            quote = Quote(
                material_id=mat.id,
                supplier_id=supplier.id if supplier else None,
                project_id=project.id if project else None,
                unit_price=price,
                unit_price_excl_tax=price_excl,
                quantity=qty,
                total_price=total_price,
                brand=brand,
                brand_tier=brand_tier,
                remark=remark,
                batch_id=batch_id,
                bid_status=bid_status,
            )
            db.add(quote)
            imported += 1

        except Exception as e:
            errors.append({"row": row_num, "reason": str(e)})
            skipped += 1

    db.commit()

    # 导入后刷新基线
    try:
        from apps.api.services.statistics import refresh_material_baselines
        refresh_material_baselines(db, category)
    except Exception:
        pass  # non-blocking

    return {
        "status": "ok" if imported > 0 else "error",
        "batch_id": batch_id,
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:50],
        "unknown_brands": sorted(unknown_brands),
        "supplier_ids": sorted(supplier_ids),
    }


# ─── Column detection ────────────────────────────────────────────────────────

_COLUMN_PATTERNS = {
    "name": [r"名称", r"品名", r"物料名", r"设备名", r"材料名"],
    "spec": [r"规格型号", r"规格", r"型号"],
    "brand": [r"品牌", r"厂家", r"制造商"],
    "supplier": [r"供应商", r"投标单位", r"报价单位", r"厂商名称"],
    "material_type": [r"材质", r"材料", r"牌号"],
    "unit": [r"单位", r"计量"],
    "price": [r"含税单价", r"含税.*单价", r"单价.*含税", r"价税合计", r"含税价", r"单价"],
    "price_excl": [r"不含税单价", r"不含税.*单价", r"单价.*不含税", r"不含税价"],
    "quantity": [r"数量", r"工程量", r"数 量"],
    "remark": [r"备注", r"说明", r"备 注"],
    "sub_category": [r"子类", r"类别", r"分类"],
}


def _detect_columns(columns: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for field, patterns in _COLUMN_PATTERNS.items():
        result[field] = None
        for pattern in patterns:
            for col in columns:
                if re.search(pattern, str(col)):
                    result[field] = col
                    break
            if result[field]:
                break
    return result


def _get_col_value(row, col_map: dict, field: str) -> str | None:
    col = col_map.get(field)
    if not col:
        return None
    val = row.get(col)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    return s if s and s != "nan" else None
