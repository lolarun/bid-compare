"""Operation log model for audit trail."""

from sqlalchemy import Column, DateTime, Integer, String, Text

from apps.api.core.database import Base
from apps.api.models._base import _now


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user = Column(String(64), nullable=False, index=True)
    module = Column(String(64), nullable=False, index=True)
    action = Column(String(64), nullable=False)
    target = Column(String(256), default="")
    result = Column(String(8), nullable=False, default="成功")  # 成功/失败
    remark = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=_now, index=True)
