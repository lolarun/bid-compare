"""Material (物料主数据) ORM model."""

from sqlalchemy import Column, Integer, Float, String, DateTime, JSON, Index
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Material(Base):
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    material_code = Column(String(32), unique=True, nullable=True, index=True)

    # Layer 1 — 基础属性
    standard_name = Column(String(200), nullable=False)
    profession = Column(String(20), nullable=False, index=True)
    category = Column(String(20), nullable=False, index=True)
    sub_category = Column(String(40), default="")
    spec = Column(String(200), default="")
    material_type = Column(String(100), default="")
    unit = Column(String(10), default="")
    brand = Column(String(100), default="")
    exec_standard = Column(String(100), default="")

    # Layer 2 — 扩展属性 (JSON per category)
    extended_attrs = Column(JSON, default=dict)

    # Layer 3 — 采购参考 (auto-computed)
    ref_price_reasonable_low = Column(Float, nullable=True)  # IQR过滤后最小值（合理史低）
    ref_price_low = Column(Float, nullable=True)
    ref_price_avg = Column(Float, nullable=True)
    ref_price_median = Column(Float, nullable=True)
    ref_price_high = Column(Float, nullable=True)
    price_cv = Column(Float, nullable=True)
    deviation_threshold = Column(Float, nullable=True)
    recommended_brands = Column(JSON, default=list)
    supplier_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="material")

    __table_args__ = (
        Index("ix_mat_prof_cat", "profession", "category"),
    )

    def __repr__(self):
        return f"<Material {self.material_code} {self.standard_name}>"
