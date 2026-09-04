"""Supplier (供应商) ORM model."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    short_name: Mapped[str | None] = mapped_column(String(50), default="")
    contact: Mapped[str | None] = mapped_column(String(100), default="")
    phone: Mapped[str | None] = mapped_column(String(30), default="")
    categories: Mapped[Any] = mapped_column(JSON, default=list, nullable=True)
    supplier_type: Mapped[str | None] = mapped_column(String(20), default="供应商")
    win_count: Mapped[int | None] = mapped_column(Integer, default=0)
    cooperation_score: Mapped[float | None] = mapped_column(Float, default=0.0)
    is_new: Mapped[int | None] = mapped_column(Integer, default=1)
    remark: Mapped[str | None] = mapped_column(Text, default="")

    # P0 清洗标记（在现有数据库通过 _ensure_sqlite_schema ADD COLUMN 添加）
    # merge_status: active（正常）/ merged（已合并入其他供应商）/ inactive（已停用）
    merge_status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 仅 merge_status='merged' 时非空，指向 canonical supplier_id
    merged_into_supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="supplier")
    aliases = relationship(
        "SupplierAlias", back_populates="supplier", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Supplier {self.name}>"
