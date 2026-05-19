"""Project (项目) ORM model."""

from sqlalchemy import Column, Integer, String, Text, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, index=True)
    code = Column(String(50), default="")
    location = Column(String(200), default="")
    status = Column(String(20), default="进行中")
    remark = Column(Text, default="")

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="project")

    __table_args__ = (
        UniqueConstraint("name", "code", name="uq_project_name_code"),
    )

    def __repr__(self):
        return f"<Project {self.name}>"
