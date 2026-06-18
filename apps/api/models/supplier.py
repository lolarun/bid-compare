"""Supplier (供应商) ORM model."""

from sqlalchemy import Column, Integer, Float, String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, unique=True, index=True)
    short_name = Column(String(50), default="")
    contact = Column(String(100), default="")
    phone = Column(String(30), default="")
    categories = Column(JSON, default=list)
    supplier_type = Column(String(20), default="供应商")
    win_count = Column(Integer, default=0)
    cooperation_score = Column(Float, default=0.0)
    is_new = Column(Integer, default=1)
    remark = Column(Text, default="")

    # P0 清洗标记（在现有数据库通过 _ensure_sqlite_schema ADD COLUMN 添加）
    # merge_status: active（正常）/ merged（已合并入其他供应商）/ inactive（已停用）
    merge_status = Column(String(20), nullable=False, default="active")
    # 仅 merge_status='merged' 时非空，指向 canonical supplier_id
    merged_into_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="supplier")
    aliases = relationship(
        "SupplierAlias", back_populates="supplier", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Supplier {self.name}>"
