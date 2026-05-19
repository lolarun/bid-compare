"""Quote (报价记录) ORM model."""

from sqlalchemy import Column, Integer, Float, String, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Quote(Base):
    __tablename__ = "quotes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)

    # 价格
    unit_price = Column(Float, nullable=True)
    unit_price_excl_tax = Column(Float, nullable=True)
    tax_rate = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)
    total_price = Column(Float, nullable=True)

    # 商务
    brand = Column(String(100), default="")
    brand_tier = Column(String(20), default="")
    remark = Column(Text, default="")
    quote_date = Column(String(20), default="")

    # 导入批次
    batch_id = Column(String(50), default="")

    # 分析标记
    deviation_pct = Column(Float, nullable=True)
    alert_level = Column(String(10), default="")
    baseline_type = Column(String(20), default="median")

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    material = relationship("Material", back_populates="quotes")
    supplier = relationship("Supplier", back_populates="quotes")
    project = relationship("Project", back_populates="quotes")

    __table_args__ = (
        Index("ix_quote_mat_sup", "material_id", "supplier_id"),
    )

    def __repr__(self):
        return f"<Quote mat={self.material_id} sup={self.supplier_id} price={self.unit_price}>"
