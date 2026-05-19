"""BrandTier (品牌档位映射) ORM model."""
from sqlalchemy import Column, Integer, String, DateTime, Index
from apps.api.core.database import Base
from apps.api.models._base import _now


class BrandTier(Base):
    __tablename__ = "brand_tiers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    brand_name = Column(String(100), nullable=False)
    tier = Column(String(20), nullable=False)        # 一档/二档/三档
    category = Column(String(20), nullable=True)    # None = 通用

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_brand_tier_name_cat", "brand_name", "category"),
    )

    def __repr__(self):
        return f"<BrandTier {self.brand_name}={self.tier}>"
