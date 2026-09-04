"""SubmissionBasis ORM model —— 一份报价的**份级口径**（可比性基准）。

设计见 `.claude/plans/comparability-basis-dimensions.md`。一句话：真实招标里
决定"能不能比"的不止含税/不含税，还有**交付范围、原材料价格基准、付款条件**。
真实材料（docs/test2 临港中科院）里，母线第一轮四家中一家报「不含安装」
827,034、其余三家「含安装」，四家的铜价基准还各不相同——把这四个数放在一起
排序就是静默混比。

三条结构性决定：

1. **独立表，一行一个 (submission, dim)**，不是 BidSubmission 上的一个 JSON 列。
   口径要按维度查询（"这一轮谁的交付范围不一致"）、要能逐维加约束，JSON 列做
   这两件事都别扭。

2. **行级口径不在这张表里。** 付款条件永远是整份的，导体规格（`4*45`）永远是
   行的。混成一层，要么份级字段在每行重复，要么行级差异被份级平均掉。行级口径
   走 BidQuoteLine（P3）。

3. **`status` 必须能区分"模型没抽到"和"原文里确实没有"**（用户 2026-09-03 决策 2
   的附加约束）。前者是抽取失败要重试/人工补，后者是投标方没声明、本身就是疑点。
   两者混成一个 null，就永远查不出模型漏抽了多少。所以是四态，不用 null 兜底。

**本模型不做任何折算。** 不把「不含安装」加上估算安装费、不按铜价换算总价、
不给付款条件做资金成本折现——折算需要投标方没给的数（安装单价、含铜占比、
资金成本率），凭空造出来的可比性比不可比更危险（同 price_basis.py 那句
"绝不 ×1.13 / ÷数量自行推导"）。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.api.core.database import Base
from apps.api.models._base import _now

# ── 口径维度 ────────────────────────────────────────────────────────────────
DIM_DELIVERY_SCOPE = "delivery_scope"          # 交付范围：含安装/不含安装/含运费到场
DIM_COMMODITY_BENCHMARK = "commodity_benchmark"  # 原材料基准：{material, price, unit}
DIM_PAYMENT_TERMS = "payment_terms"            # 付款条件：结构化槽位
DIMENSIONS = (DIM_DELIVERY_SCOPE, DIM_COMMODITY_BENCHMARK, DIM_PAYMENT_TERMS)

# ── 状态 ────────────────────────────────────────────────────────────────────
#: 模型抽出了候选值，**未经确认**。门禁不吃它（未知 ≠ 一致）。
STATUS_EXTRACTED = "extracted"
#: 原文里确实没有这个维度的声明。这是**业务事实**，本身就是疑点，不是失败。
STATUS_NOT_PRESENT = "not_present"
#: 抽取失败（模型报错/超时/输出不可解析）。要重试或人工补，不能当成"没有"。
STATUS_EXTRACTION_FAILED = "extraction_failed"
#: 人工确认过。**只有这一态能进一致性判定**。
STATUS_CONFIRMED = "confirmed"
STATUSES = (
    STATUS_EXTRACTED,
    STATUS_NOT_PRESENT,
    STATUS_EXTRACTION_FAILED,
    STATUS_CONFIRMED,
)


class SubmissionBasis(Base):
    """一份报价在某个口径维度上的取值。"""

    __tablename__ = "submission_basis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bid_submissions.id"), nullable=False, index=True,
    )
    #: DIMENSIONS 之一。写入前由 service 校验，与 quote_round_service 校验 stage 同理。
    dim: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=STATUS_EXTRACTED, index=True,
    )

    #: 归一后的结构化值。形状按 dim 而定：
    #:   delivery_scope       {"scope": "excl_installation"}
    #:   commodity_benchmark  {"material": "铜", "price": 73410, "unit": "元/吨"}
    #:   payment_terms        {"advance_pct": .., "retention_pct": .., ...}
    #: `not_present` / `extraction_failed` 时为 None——**不用空字典冒充"没有"**。
    value: Mapped[Any] = mapped_column(JSON, nullable=True)

    #: 原文。界面上一律"原文可见"，不给只显示归一值的视图——归一表出错时，
    #: 原文是唯一能让人看出来的东西。
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: 来源 {page, row}，与 TenderAnchor.source_ref 同形，便于回溯到文件位置。
    source_ref: Mapped[Any] = mapped_column(JSON, nullable=True)

    #: 抽取者标识（模型名+版本 / "manual"）。归一表或模型换代后要能回答
    #: "这批值是谁抽的"。
    extracted_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=_now, onupdate=_now,
    )

    __table_args__ = (
        # 一份报价在一个维度上只有一个当前取值。改值是 UPDATE，不是插第二行——
        # 历史留在操作日志里，不靠这张表堆版本。
        UniqueConstraint("submission_id", "dim", name="uq_submission_basis_dim"),
    )

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"<SubmissionBasis submission={self.submission_id} dim={self.dim} "
            f"status={self.status}>"
        )
