"""SupplierAlias — 供应商别名表。

用途：将供应商在不同文件/OCR 结果中出现的各种名称形式，
统一映射回 canonical supplier_id，替代纯模糊字符串匹配。

alias_type 取值：
  legal_name   营业执照全称
  short_name   日常简称
  filename     出现在文件名中的形式（噪声清除后）
  historical   历史 OCR 识别到、人工确认的别名

唯一约束：UNIQUE(supplier_id, normalized_alias, alias_type)
  - 同一供应商可保留多条不同来源证据（short_name + filename 均为"凯硕新正"可并存）
  - 不同供应商的相同 normalized_alias 在排查期暂时允许，解决后标记 active=0

normalized_alias 生成规则（见 _normalize_alias()）：
  1. 去文件扩展名
  2. 去噪声词（投标文件/报价单/第一轮/终稿等）
  3. 去日期模式
  4. 转小写，去首尾空格
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.core.database import Base
from apps.api.models._base import _now

# 文件名噪声词（顺序敏感：长词先匹配，避免子串残留）
_NOISE_WORDS = [
    "投标文件", "报价文件", "报价单", "采购清单", "招标文件",
    "第一轮", "第二轮", "第一次", "第二次",
    "终稿", "定稿", "修改版", "最终版",
    "v3", "v2", "v1",
]
_NOISE_EXT = re.compile(r"\.(pdf|xlsx?|docx?)$", re.IGNORECASE)
_NOISE_DATE = re.compile(r"\d{4}[-_年]\d{1,2}[-_月]\d{1,2}[日]?|\d{6,8}")
_NOISE_EMPTY_BRACKETS = re.compile(r"[（(]\s*[)）]")


def normalize_alias(text: str) -> str:
    """将别名文本规范化，用于唯一约束和查找匹配。

    调用方在保存 SupplierAlias 前必须先调用此函数生成 normalized_alias。
    """
    s = text.strip()
    s = _NOISE_EXT.sub("", s)
    for word in _NOISE_WORDS:
        s = s.replace(word, "")
    s = _NOISE_DATE.sub("", s)
    s = _NOISE_EMPTY_BRACKETS.sub("", s)
    return s.strip().lower()


class SupplierAlias(Base):
    """供应商别名记录。"""

    __tablename__ = "supplier_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    alias: Mapped[str] = mapped_column(String(300), nullable=False, default="")       # 原始文本，保留供展示/溯源
    normalized_alias: Mapped[str] = mapped_column(String(300), nullable=False)         # 规范化文本，用于查找
    alias_type: Mapped[str] = mapped_column(String(20), nullable=False, default="historical")
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)            # 1=启用，0=禁用
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)        # 0~1
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")   # 'system_init'/'user:xxx'/'ocr_auto'
    source_reference: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # 来源说明
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)

    supplier = relationship("Supplier", back_populates="aliases")

    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "normalized_alias", "alias_type",
            name="uq_sa_supplier_normalized_type",
        ),
        Index("ix_sa_normalized", "normalized_alias"),
        Index("ix_sa_supplier_id", "supplier_id"),
        Index("ix_sa_type", "alias_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<SupplierAlias supplier_id={self.supplier_id} "
            f"type={self.alias_type!r} alias={self.alias!r:.40}>"
        )
