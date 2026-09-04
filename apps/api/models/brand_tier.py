"""BrandTier (品牌档位 + 合格品牌清单) ORM model."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class BrandTier(Base):
    __tablename__ = "brand_tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)        # 国产/合资
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)     # None = 通用

    # 合格品牌清单
    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 品牌名归一化：别名指向 canonical_name，canonical 记录自指
    # 例：brand_name="开兹", canonical_name="KITZ", alias_of="KITZ"
    canonical_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # None = 自身即标准名
    alias_of: Mapped[str | None] = mapped_column(String(100), nullable=True)        # None = 非别名

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_brand_tier_name_cat", "brand_name", "category"),
        Index("ix_brand_tier_canonical", "canonical_name", "category"),
    )

    def __repr__(self):
        return f"<BrandTier {self.brand_name}={self.tier} approved={self.is_approved}>"
