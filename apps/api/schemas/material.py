"""Material Pydantic schemas."""

from datetime import datetime
from pydantic import BaseModel, Field


class MaterialBase(BaseModel):
    standard_name: str
    profession: str
    category: str
    sub_category: str = ""
    spec: str = ""
    material_type: str = ""
    unit: str = ""
    brand: str = ""
    exec_standard: str = ""
    extended_attrs: dict = Field(default_factory=dict)


class MaterialCreate(MaterialBase):
    material_code: str | None = None


class MaterialUpdate(BaseModel):
    standard_name: str | None = None
    spec: str | None = None
    material_type: str | None = None
    unit: str | None = None
    brand: str | None = None
    exec_standard: str | None = None
    extended_attrs: dict | None = None
    sub_category: str | None = None


class MaterialOut(MaterialBase):
    id: int
    material_code: str
    ref_price_low: float | None = None
    ref_price_avg: float | None = None
    ref_price_median: float | None = None
    ref_price_high: float | None = None
    price_cv: float | None = None
    deviation_threshold: float | None = None
    recommended_brands: list[str] = Field(default_factory=list)
    supplier_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class StandardizeRequest(BaseModel):
    text: str
    category: str | None = None


class StandardizeResult(BaseModel):
    original: str
    standardized: str
    changes: list[str]


class ExtendedAttrField(BaseModel):
    key: str
    label: str
    source: str
    role: str


class ExtendedAttrSchema(BaseModel):
    category: str
    fields: list[ExtendedAttrField]
