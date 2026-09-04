"""Material (物料主数据) ORM model."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)

    # Layer 1 — 基础属性
    standard_name: Mapped[str] = mapped_column(String(200), nullable=False)
    profession: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    sub_category: Mapped[str | None] = mapped_column(String(40), default="")
    spec: Mapped[str | None] = mapped_column(String(200), default="")
    material_type: Mapped[str | None] = mapped_column(String(100), default="")
    unit: Mapped[str | None] = mapped_column(String(10), default="")
    brand: Mapped[str | None] = mapped_column(String(100), default="")
    exec_standard: Mapped[str | None] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)

    # Layer 2 — 扩展属性 (JSON per category)
    extended_attrs: Mapped[Any] = mapped_column(JSON, default=dict, nullable=True)

    # Layer 3 — 采购参考 (auto-computed)
    ref_price_reasonable_low: Mapped[float | None] = mapped_column(Float, nullable=True)  # IQR过滤后最小值（合理史低）
    ref_price_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_price_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_price_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_price_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_cv: Mapped[float | None] = mapped_column(Float, nullable=True)
    deviation_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_brands: Mapped[Any] = mapped_column(JSON, default=list, nullable=True)
    supplier_count: Mapped[int | None] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="material")

    __table_args__ = (
        Index("ix_mat_prof_cat", "profession", "category"),
    )

    def __repr__(self):
        return f"<Material {self.material_code} {self.standard_name}>"
