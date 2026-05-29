"""Common/shared Pydantic schemas."""

from pydantic import BaseModel


class PaginatedResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list


class ImportResult(BaseModel):
    status: str
    batch_id: str
    imported: int
    skipped: int
    errors: list[dict] = []
    unknown_brands: list[str] = []
    supplier_ids: list[int] = []
