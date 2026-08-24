"""Material CRUD API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from apps.api.core.database import get_db
from apps.api.core.config import PROFESSION_ABBR, CATEGORY_ABBR, PROFESSION_MAP, EXTENDED_ATTR_SCHEMAS, NAMING_STANDARDS
from apps.api.models import Material
from apps.api.schemas import (
    MaterialCreate, MaterialUpdate, MaterialOut,
    StandardizeRequest, StandardizeResult,
    ExtendedAttrSchema,
)
from apps.api.services.ingestion.standardize import standardize_name

router = APIRouter(prefix="/api/materials", tags=["materials"])


def _generate_code(db: Session, profession: str, category: str) -> str:
    prof_abbr = PROFESSION_ABBR.get(profession, "OT")
    cat_abbr = CATEGORY_ABBR.get(category, "OTH")
    last = db.scalar(select(Material).where(
        Material.material_code.like(f"{prof_abbr}-{cat_abbr}-%")
    ).order_by(Material.material_code.desc()))

    seq = int(last.material_code.split("-")[-1]) + 1 if last else 1
    return f"{prof_abbr}-{cat_abbr}-{seq:05d}"


@router.get("", response_model=dict)
def list_materials(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    profession: str | None = None,
    category: str | None = None,
    sub_category: str | None = None,
    keyword: str | None = None,
    include_disabled: bool = False,
    db: Session = Depends(get_db),
):
    stmt = select(Material)
    if not include_disabled:
        stmt = stmt.where(or_(Material.status.is_(None), Material.status != "disabled"))
    if profession:
        stmt = stmt.where(Material.profession == profession)
    if category:
        stmt = stmt.where(Material.category == category)
    if sub_category:
        stmt = stmt.where(Material.sub_category == sub_category)
    if keyword:
        stmt = stmt.where(
            Material.standard_name.contains(keyword)
            | Material.spec.contains(keyword)
            | Material.material_code.contains(keyword)
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.order_by(Material.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [MaterialOut.model_validate(i).model_dump() for i in items],
    }


@router.get("/supported-categories", response_model=list[str])
def list_supported_categories():
    """系统支持的全部品类（`config.ALL_CATEGORIES`），与物料主数据里**有没有
    数据无关**。

    跟下面的 `/categories` 是两件事：那个按 Material 表分组统计"实际有多少条
    物料"，一个全新库里会是空的；这个是词表本身，供前端做品类选择器。
    2026-08-23 新增——此前前端没有任何手动选择品类的入口，而 `batch-confirm`
    的错误提示写着"请先选择本报价所属品类"，指向一个不存在的控件：招标文件
    不带采购清单时用户到那一步就是死路。选择器的选项必须来自后端这一份词表，
    在前端另抄一份迟早会跟 `PROFESSION_MAP` 漂移。
    """
    from apps.api.core.config import ALL_CATEGORIES

    return ALL_CATEGORIES


@router.get("/categories", response_model=list[dict])
def list_categories(db: Session = Depends(get_db)):
    rows = db.execute(select(
        Material.profession, Material.category,
        func.count(Material.id).label("count"),
    ).where(
        or_(Material.status.is_(None), Material.status != "disabled")
    ).group_by(Material.profession, Material.category)).all()
    return [
        {"profession": r.profession, "category": r.category, "count": r.count}
        for r in rows
    ]


@router.post("/standardize", response_model=StandardizeResult)
def standardize(body: StandardizeRequest):
    """Standardize a material name/spec string."""
    return standardize_name(body.text, body.category)


@router.get("/extended-schema/{category}", response_model=ExtendedAttrSchema)
def get_extended_schema(category: str):
    """Get the extended attribute schema for a given category."""
    fields = EXTENDED_ATTR_SCHEMAS.get(category)
    if fields is None:
        raise HTTPException(404, f"No extended schema for category '{category}'")
    return {"category": category, "fields": fields}


@router.get("/naming-standards/{category}")
def get_naming_standards(category: str):
    """Get the naming standard reference for a given category."""
    standards = NAMING_STANDARDS.get(category)
    if standards is None:
        raise HTTPException(404, f"No naming standards for category '{category}'")
    return {"category": category, "dimensions": standards}


@router.get("/{material_id}", response_model=MaterialOut)
def get_material(material_id: int, db: Session = Depends(get_db)):
    mat = db.get(Material, material_id)
    if not mat:
        raise HTTPException(404, "Material not found")
    return mat


@router.post("", response_model=MaterialOut, status_code=201)
def create_material(body: MaterialCreate, db: Session = Depends(get_db)):
    profession = body.profession or PROFESSION_MAP.get(body.category, "其他")
    code = body.material_code or _generate_code(db, profession, body.category)

    mat = Material(
        material_code=code,
        standard_name=body.standard_name,
        profession=profession,
        category=body.category,
        sub_category=body.sub_category,
        spec=body.spec,
        material_type=body.material_type,
        unit=body.unit,
        brand=body.brand,
        exec_standard=body.exec_standard,
        status=body.status or "active",
        extended_attrs=body.extended_attrs,
    )
    db.add(mat)
    db.commit()
    db.refresh(mat)
    return mat


@router.put("/{material_id}", response_model=MaterialOut)
def update_material(material_id: int, body: MaterialUpdate, db: Session = Depends(get_db)):
    mat = db.get(Material, material_id)
    if not mat:
        raise HTTPException(404, "Material not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(mat, field, value)

    db.commit()
    db.refresh(mat)
    return mat


@router.post("/{material_id}/disable", response_model=MaterialOut)
def disable_material(material_id: int, db: Session = Depends(get_db)):
    mat = db.get(Material, material_id)
    if not mat:
        raise HTTPException(404, "Material not found")

    mat.status = "disabled"
    db.commit()
    db.refresh(mat)
    return mat


@router.delete("/{material_id}", status_code=204)
def delete_material(material_id: int, db: Session = Depends(get_db)):
    mat = db.get(Material, material_id)
    if not mat:
        raise HTTPException(404, "Material not found")
    db.delete(mat)
    db.commit()
