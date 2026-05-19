"""Quote Pydantic schemas."""

from datetime import datetime
from pydantic import BaseModel


class QuoteBase(BaseModel):
    material_id: int
    supplier_id: int | None = None
    project_id: int | None = None
    unit_price: float | None = None
    unit_price_excl_tax: float | None = None
    tax_rate: float | None = None
    quantity: float | None = None
    total_price: float | None = None
    brand: str = ""
    brand_tier: str = ""
    remark: str = ""
    quote_date: str = ""


class QuoteCreate(QuoteBase):
    pass


class QuoteUpdate(BaseModel):
    unit_price: float | None = None
    unit_price_excl_tax: float | None = None
    tax_rate: float | None = None
    quantity: float | None = None
    total_price: float | None = None
    brand: str | None = None
    remark: str | None = None
    quote_date: str | None = None
    supplier_id: int | None = None
    project_id: int | None = None


class QuoteOut(QuoteBase):
    id: int
    batch_id: str = ""
    deviation_pct: float | None = None
    alert_level: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
