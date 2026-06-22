"""BidSubmission + BidQuoteLine — 投标暂存层。

比价流程中供应商报价的暂存容器，与 Quote（历史价格表）完全隔离：
  - batch-confirm 只写这两张表，不再写 quotes / materials / suppliers。
  - 对齐、复核、矩阵、导出全程读 BidQuoteLine。
  - 显式调用 archive-prices 后才写 Quote，且仅当 material_id IS NOT NULL。
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, Float, String, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship

from apps.api.core.database import Base
from apps.api.models._base import _now


class BidSubmission(Base):
    """一次供应商报价的暂存头记录。

    status 状态机：
      pending          → 已暂存，待人工审核
      confirmed        → 采购员审核通过（可触发归档）
      archived         → 全部行归档成功（Quote 已写入）
      partially_archived → 部分行因 material_id=NULL 跳过
      rejected         → 采购员拒绝，不入历史
    """

    __tablename__ = "bid_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("extraction_jobs.id"), nullable=False, index=True)
    # 弱关联：supplier_id 可为 NULL（陌生供应商可完成比价，归档时才要求绑定正式供应商）
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True, index=True)
    supplier_raw_name = Column(String(200), nullable=False, default="")  # OCR 原始名 / 显示名，必填
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True, index=True)
    batch_id = Column(String(100), nullable=False, unique=True)  # 幂等键
    status = Column(String(30), nullable=False, default="pending")
    bid_status = Column(String(30), nullable=False, default="")  # confirming / confirmed 等
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    lines = relationship(
        "BidQuoteLine", back_populates="submission", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<BidSubmission id={self.id} supplier_id={self.supplier_id} status={self.status!r}>"


class BidQuoteLine(Base):
    """一条暂存报价行。

    material_id 可为 NULL：
      - NULL 时仍可通过 canonical / standard_name / spec 做项目内软对齐。
      - archive-prices 时 material_id=NULL 的行静默跳过（不报错，不创建 Material）。

    archived_quote_id 归档后回填，指向正式 Quote 记录；NULL 表示未归档。
    """

    __tablename__ = "bid_quote_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    submission_id = Column(
        Integer, ForeignKey("bid_submissions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=True, index=True)

    raw_name = Column(String(500), nullable=False, default="")   # OCR 原始品名
    standard_name = Column(String(200), nullable=False, default="")
    category = Column(String(50), nullable=False, default="")
    spec = Column(String(200), nullable=False, default="")
    unit = Column(String(20), nullable=False, default="")
    qty = Column(Float, nullable=True)
    unit_price = Column(Float, nullable=True)
    unit_price_excl_tax = Column(Float, nullable=True)
    tax_rate = Column(Float, nullable=True)
    total_price = Column(Float, nullable=True)
    brand = Column(String(100), nullable=False, default="")
    brand_tier = Column(String(20), nullable=False, default="")
    remark = Column(Text, nullable=False, default="")
    quote_date = Column(String(20), nullable=False, default="")

    # 结构化键（valve_type/DN/PN 等），用于 material_id=NULL 时的项目内软对齐
    canonical = Column(JSON, nullable=True)
    # 完整 OCR 证据（与 Quote.extraction_meta_json 格式相同）
    extraction_meta = Column(JSON, nullable=True)

    deviation_pct = Column(Float, nullable=True)
    alert_level = Column(String(10), nullable=False, default="")

    # 归档后回填，指向正式 Quote；NULL 表示未归档或 material_id=NULL 跳过
    archived_quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=True)

    created_at = Column(DateTime, default=_now)
    # 行级审计时序（P1-3）：人工修正/重匹配等任意行变更自动刷新（ORM onupdate）。
    # 存量行由迁移回填为 created_at。
    updated_at = Column(DateTime, default=_now, onupdate=_now)
    # 确认时的行类型快照（P1-3）: quote_line|section_header|remark|invalid|subtotal|grand_total
    # 存量行由迁移回填为 'quote_line'（非 quote_line 行在 confirm 阶段被过滤不入库）。
    row_type = Column(String(32), nullable=True, default="quote_line")

    submission = relationship("BidSubmission", back_populates="lines")

    def __repr__(self) -> str:
        return (
            f"<BidQuoteLine id={self.id} submission_id={self.submission_id} "
            f"name={self.raw_name!r:.30} material_id={self.material_id}>"
        )
