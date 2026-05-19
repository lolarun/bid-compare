"""Project Pydantic schemas."""

from datetime import datetime
from pydantic import BaseModel


class ProjectBase(BaseModel):
    name: str
    code: str = ""
    location: str = ""
    status: str = "进行中"
    remark: str = ""


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    location: str | None = None
    status: str | None = None
    remark: str | None = None


class ProjectOut(ProjectBase):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
