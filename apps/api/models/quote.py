"""Quote (报价记录) ORM model."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(Integer, ForeignKey("materials.id"), nullable=False, index=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)

    # 价格
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price_excl_tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 商务
    brand: Mapped[str | None] = mapped_column(String(100), default="")
    brand_tier: Mapped[str | None] = mapped_column(String(20), default="")
    remark: Mapped[str | None] = mapped_column(Text, default="")
    quote_date: Mapped[str | None] = mapped_column(String(20), default="")

    # 导入批次
    batch_id: Mapped[str | None] = mapped_column(String(50), default="")
    bid_status: Mapped[str | None] = mapped_column(String(20), default="")

    # 分析标记
    deviation_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_level: Mapped[str | None] = mapped_column(String(10), default="")
    baseline_type: Mapped[str | None] = mapped_column(String(20), default="median")

    # 行级抽取证据 (供 LLM 供应商填表代理「像人一样看报价」+ 审计)
    # {extraction_job_id, source_ref, raw_material, raw_spec, raw_unit, raw_remark,
    #  canonical, validation_warning}
    extraction_meta_json: Mapped[Any] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    material = relationship("Material", back_populates="quotes")
    supplier = relationship("Supplier", back_populates="quotes")
    project = relationship("Project", back_populates="quotes")

    __table_args__ = (
        Index("ix_quote_mat_sup", "material_id", "supplier_id"),
    )

    def __repr__(self):
        return f"<Quote mat={self.material_id} sup={self.supplier_id} price={self.unit_price}>"
