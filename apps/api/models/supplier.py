"""Supplier (供应商) ORM model."""

from sqlalchemy import Column, Integer, Float, String, Text, DateTime, JSON
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

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="supplier")

    def __repr__(self):
        return f"<Supplier {self.name}>"
