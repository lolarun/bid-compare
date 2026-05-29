"""User Pydantic schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str
    password: str
    nickname: str = ""
    role: Literal["管理员", "比价员", "查看者"] = "比价员"
    email: str = ""
    phone: str = ""


class UserUpdate(BaseModel):
    nickname: str | None = None
    role: Literal["管理员", "比价员", "查看者"] | None = None
    email: str | None = None
    phone: str | None = None
    password: str | None = None


class UserOut(BaseModel):
    id: int
    username: str
    nickname: str
    role: str
    email: str
    phone: str
    status: str
    last_login: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user) -> "UserOut":
        last = user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else None
        return cls(
            id=user.id,
            username=user.username,
            nickname=user.nickname,
            role=user.role,
            email=user.email,
            phone=user.phone,
            status=user.status,
            last_login=last,
            created_at=user.created_at,
        )
