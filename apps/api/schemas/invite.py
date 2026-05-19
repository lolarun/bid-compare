"""Pydantic schemas for /api/invite endpoints."""

from typing import Any

from pydantic import BaseModel, Field


class TenderItem(BaseModel):
    """A single line in the tender's material list."""

    name: str = ""
    category: str = ""
    spec: str = ""
    unit: str = ""
    quantity: float | None = None
    remark: str = ""


class RecommendRequest(BaseModel):
    tender_items: list[dict[str, Any]] = Field(default_factory=list)
    top_n: int = 5
    project_id: int | None = None


class RecommendReason(BaseModel):
    history_count: int = 0
    history_score: float = 0
    avg_deviation_pct: float | None = None
    price_score: float = 0
    overall_score: float = 0
    brand_score: float = 0
    summary: str = ""


class SupplierRecommendation(BaseModel):
    supplier_id: int
    supplier_name: str
    score: float
    rank: int
    reason: RecommendReason


class RecommendResponse(BaseModel):
    categories: list[str]
    recommendations: list[SupplierRecommendation]


class SaveInvitationsRequest(BaseModel):
    tender_id: int | None = None  # if None, a new TenderDocument is created
    job_id: str | None = None     # optionally link to the source ExtractionJob
    project_id: int | None = None
    project_name: str = ""
    project_code: str = ""
    tender_date: str = ""
    deadline: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)
    supplier_ids: list[int]


class SavedInvitation(BaseModel):
    id: int
    supplier_id: int
    supplier_name: str
    rank: int | None = None
    score: float | None = None
    status: str


class SaveInvitationsResponse(BaseModel):
    tender_id: int
    invitations: list[SavedInvitation]
