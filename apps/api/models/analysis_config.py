"""AnalysisConfig (比价配置) ORM model."""

from sqlalchemy import Column, Integer, String, DateTime, JSON

from apps.api.core.database import Base
from apps.api.models._base import _now


class AnalysisConfig(Base):
    __tablename__ = "analysis_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False, index=True)
    value = Column(JSON, nullable=False)
    description = Column(String(200), default="")

    updated_at = Column(DateTime, default=_now, onupdate=_now)

    def __repr__(self):
        return f"<AnalysisConfig {self.key}>"
