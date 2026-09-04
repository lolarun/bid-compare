"""AnalysisConfig (比价配置) ORM model."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class AnalysisConfig(Base):
    __tablename__ = "analysis_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), default="")

    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    def __repr__(self):
        return f"<AnalysisConfig {self.key}>"
