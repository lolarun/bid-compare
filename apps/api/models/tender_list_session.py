"""TenderListSession — 已确认的招标采购清单版本。

每次用户确认一个采购清单 xlsx，保存一个版本记录。
(project_id, category) 下保留全量历史；is_current=True 的是当前版本。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now


class TenderListSession(Base):
    """一个经用户确认的采购清单版本。"""

    __tablename__ = "tender_list_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(50), default="", nullable=False)

    file_name: Mapped[str | None] = mapped_column(String(500), default="")
    source_type: Mapped[str | None] = mapped_column(String(20), default="excel")  # excel | pdf — 基础清单来源
    anchors_total: Mapped[int | None] = mapped_column(Integer, default=0)
    anchors_json: Mapped[Any] = mapped_column(JSON, nullable=True)   # list[dict] — TenderAnchor 序列化

    # PDF 招标文件第 13 页：业主品牌要求 + 投标单位参与品牌映射（PDF 来源时填充）
    brand_requirement: Mapped[Any] = mapped_column(JSON, nullable=True)   # list[{brand_en, brand_cn}]
    supplier_brand_map: Mapped[Any] = mapped_column(JSON, nullable=True)  # {supplier_name 或 supplier_id: brand}

    version: Mapped[int | None] = mapped_column(Integer, default=1)         # 同 (project_id, category) 下自增
    is_current: Mapped[bool | None] = mapped_column(Boolean, default=True)   # 每次 confirm 时将旧版置 False
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str | None] = mapped_column(String(20), default="confirmed")  # preview | confirmed
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # supplier_ids confirmed for this bid-comparison session (persisted on tender-list/match)
    confirmed_supplier_ids: Mapped[Any] = mapped_column(JSON, nullable=True)  # list[int]

    # submission_ids used during the most recent tender-list/match call
    # All downstream endpoints (anchor-review, residue, llm-fill) read from here
    # so they always operate on the same batch — no self-guessing.
    used_submission_ids: Mapped[Any] = mapped_column(JSON, nullable=True)  # list[int]

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        Index("ix_tls_project_category_current", "project_id", "category", "is_current"),
    )
