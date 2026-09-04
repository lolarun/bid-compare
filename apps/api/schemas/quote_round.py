"""QuoteRound Pydantic schemas (docs/design/42)."""

from datetime import datetime

from pydantic import BaseModel


class QuoteRoundCreate(BaseModel):
    category: str = ""
    name: str = ""
    stage: str = "formal"          # pre_tender | formal
    remark: str = ""


class QuoteRoundUpdate(BaseModel):
    """Partial update. Each field is applied only when provided.

    `status` accepts "open" (reopen, closing any other open round in scope)
    or "closed". `is_final_basis` is the explicit official-basis flag
    (docs/design/42 §8 D3) — setting it True clears it on sibling rounds.
    """
    name: str | None = None
    status: str | None = None      # open | closed
    is_final_basis: bool | None = None


class QuoteRoundOut(BaseModel):
    id: int
    project_id: int
    category: str
    seq: int
    name: str
    stage: str
    status: str
    is_final_basis: bool
    tender_list_session_id: int | None = None
    confirmed_supplier_ids: list[int] | None = None
    used_submission_ids: list[int] | None = None
    created_by: str | None = None
    remark: str = ""
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
