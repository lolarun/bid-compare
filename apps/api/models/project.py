"""Project (项目) ORM model."""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(50), default="")
    location: Mapped[str | None] = mapped_column(String(200), default="")
    status: Mapped[str | None] = mapped_column(String(20), default="进行中")
    remark: Mapped[str | None] = mapped_column(Text, default="")

    # docs/design/42 §8 D1 / design/44 F3：谁建的这个项目。Nullable——存量
    # 项目和"没有登录上下文"的写路径（脚本/迁移）没有这个信息，留空诚实，
    # 不倒填一个假的创建人。P3 之前 POST /api/projects 对所有比价角色开放，
    # 这里从那时起就已经在记，不是等权限收紧才开始写。
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now, onupdate=_now)

    quotes = relationship("Quote", back_populates="project")

    __table_args__ = (
        UniqueConstraint("name", "code", name="uq_project_name_code"),
    )

    def __repr__(self):
        return f"<Project {self.name}>"
