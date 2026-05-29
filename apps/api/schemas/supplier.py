"""Supplier Pydantic schemas."""

from datetime import datetime
from pydantic import BaseModel, Field


class SupplierBase(BaseModel):
    name: str
    short_name: str = ""
    contact: str = ""
    phone: str = ""
    categories: list[str] = Field(default_factory=list)
    supplier_type: str = "供应商"
    remark: str = ""


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: str | None = None
    short_name: str | None = None
    contact: str | None = None
    phone: str | None = None
    categories: list[str] | None = None
    supplier_type: str | None = None
    remark: str | None = None


class SupplierOut(SupplierBase):
    id: int
    win_count: int = 0
    cooperation_score: float = 0.0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
