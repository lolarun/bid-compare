"""User account model."""

from sqlalchemy import Column, DateTime, Integer, String

from apps.api.core.database import Base
from apps.api.core.security import hash_password, verify_password
from apps.api.models._base import _now


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    password_salt = Column(String(32), nullable=False)
    nickname = Column(String(64), nullable=False, default="")
    role = Column(String(16), nullable=False, default="比价员")  # 管理员/比价员/查看者
    email = Column(String(128), default="")
    phone = Column(String(32), default="")
    status = Column(String(8), nullable=False, default="启用")  # 启用/停用
    last_login = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    def verify_password(self, password: str) -> bool:
        return verify_password(password, self.password_salt, self.password_hash)

    def set_password(self, password: str) -> None:
        self.password_hash, self.password_salt = hash_password(password)
