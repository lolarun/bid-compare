"""User account model."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.core.security import hash_password, verify_password
from apps.api.models._base import _now


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    password_salt: Mapped[str] = mapped_column(String(32), nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="比价员")  # 管理员/比价员/查看者
    email: Mapped[str | None] = mapped_column(String(128), default="")
    phone: Mapped[str | None] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="启用")  # 启用/停用
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    def verify_password(self, password: str) -> bool:
        return verify_password(password, self.password_salt, self.password_hash)

    def set_password(self, password: str) -> None:
        self.password_hash, self.password_salt = hash_password(password)
