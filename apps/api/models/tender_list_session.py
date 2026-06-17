"""TenderListSession — 已确认的招标采购清单版本。

每次用户确认一个采购清单 xlsx，保存一个版本记录。
(project_id, category) 下保留全量历史；is_current=True 的是当前版本。
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, ForeignKey, Index
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class TenderListSession(Base):
    """一个经用户确认的采购清单版本。"""

    __tablename__ = "tender_list_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    category = Column(String(50), default="", nullable=False)

    file_name = Column(String(500), default="")
    anchors_total = Column(Integer, default=0)
    anchors_json = Column(JSON, nullable=True)   # list[dict] — TenderAnchor 序列化

    version = Column(Integer, default=1)         # 同 (project_id, category) 下自增
    is_current = Column(Boolean, default=True)   # 每次 confirm 时将旧版置 False
    superseded_at = Column(DateTime, nullable=True)

    status = Column(String(20), default="confirmed")  # preview | confirmed
    confirmed_by = Column(String(100), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)

    # supplier_ids confirmed for this bid-comparison session (persisted on tender-list/match)
    confirmed_supplier_ids = Column(JSON, nullable=True)  # list[int]

    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_tls_project_category_current", "project_id", "category", "is_current"),
    )
