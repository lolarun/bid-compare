"""ExtractionJob ORM model — tracks async document-extraction tasks.

Lifecycle: pending → running → done / failed
Used by DocumentIngestionService to:
- enqueue (file_hash idempotency)
- run in background (via FastAPI BackgroundTasks)
- query state from frontend (polling endpoint)
"""

from sqlalchemy import Column, String, Integer, Float, Text, DateTime, JSON, Index

from apps.api.core.database import Base
from apps.api.models._base import _now


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id = Column(String(36), primary_key=True)  # UUID4 hex
    type = Column(String(16), nullable=False, index=True)  # 'tender' | 'quote'
    status = Column(String(16), nullable=False, default="pending", index=True)
    # 业务生命周期（与 OCR status 正交）：
    #   active    — 已上传/识别中/已识别待确认/失败，属于在途
    #   confirmed — 已校对入库（生成了 BidSubmission）
    #   removed   — 其 BidSubmission 已被移除（supersede）
    # compare-state 据此判定是否在途，无需反查 bid_submissions。
    lifecycle = Column(String(16), nullable=False, default="active", index=True)

    # File
    filename = Column(String(255), default="")
    file_hash = Column(String(64), index=True)  # SHA256 of content
    file_size = Column(Integer, default=0)
    file_path = Column(String(512), default="")
    mime_type = Column(String(64), default="")

    # Business context (project_id, supplier_id, category)
    context = Column(JSON, default=dict)

    # Extraction result
    result = Column(JSON, nullable=True)
    error = Column(Text, default="")
    confidence = Column(Float, nullable=True)
    progress_stage = Column(String(100), default="")
    progress_pct = Column(Integer, default=0)
    # design/24 B2：阶段内进度，弥补 progress_pct 是全局单轴、长阶段（逐页识别）
    # 长期卡在一个数字不动的问题（用户反馈 #4）。stage_total 可空——最长的那个
    # 阶段（VL 模型一次流式调用）没有总数可言，只能报"已转录 N 行"（单调递增）；
    # 有页数概念的阶段（渲染/拆分）才两个都填。前端按 stage_total is null 区分
    # "有进度条"和"只有一个递增计数"两种渲染方式。
    stage_current = Column(Integer, nullable=True)
    stage_total = Column(Integer, nullable=True)

    # Provider telemetry
    provider = Column(String(64), default="")
    tokens_used = Column(Integer, default=0)
    duration_ms = Column(Integer, default=0)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_job_type_status", "type", "status"),
        Index("ix_job_hash_type", "file_hash", "type"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtractionJob {self.id} {self.type} {self.status}>"
