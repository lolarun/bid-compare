"""Operation log model for audit trail."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(256), default="")
    result: Mapped[str] = mapped_column(String(8), nullable=False, default="成功")  # 成功/失败
    remark: Mapped[str | None] = mapped_column(Text, default="")
    # Structured domain-event payload: {event_type, identity, before, after, meta}
    payload: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now, index=True)
