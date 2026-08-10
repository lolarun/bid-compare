"""submission_eligibility.py — 「这份报价能不能进正式比价」的唯一判据。

## 为什么需要

在此之前，三处各自判断、判据不一致：

- `bid_submission_resolve` 只看 `status`（排除 rejected/superseded）；
- `bid_matrix` 读 `_checksum`，**只有 fail 阻断**，unknown 当通过；
- `routes/analysis.py` 的 match 门有自己的 6 项检查，**完全不看 checksum**。

结果是一份 checksum 失败的报价能通过 match 门进入矩阵。**同一个问题在不同入口
得到不同答案，就等于没有判据。**

## 边界（重要）

本模块**只读已持久化的状态**，不重算识别质量、不碰原始文件、不修改任何数据。
它回答的是「按已经记录下来的证据，这份报价现在能不能用」。

入库前的结构门（列错位/重复/算术/截断/声明总价）在 `quote_confirmation_service`，
那是**写入时**的判断，有回滚能力；本模块是**读取时**的判断，只能拒绝使用。
两者共享同一套语义，但触发点和处置不同——不合并成一个巨型判据，那样谁都不敢改。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import BidQuoteLine, BidSubmission, ExtractionJob
from apps.api.services.draft_integrity import BLOCKED, COLUMN_SHIFT_FLAG, OK, REVIEW

log = logging.getLogger(__name__)

# 不得进入正式比价的 submission 状态
INELIGIBLE_STATUSES = ("rejected", "superseded")


@dataclass
class Reason:
    code: str
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "evidence": self.evidence}


@dataclass
class EligibilityVerdict:
    submission_id: int
    verdict: str                       # ok | review | blocked
    reasons: list[Reason] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        """只有 BLOCKED 才是不可用。

        **REVIEW 不阻断**：它表示"有疑点、必须让人看见"，不是"有缺陷"。
        最典型的是"文件没给声明总价"——那是很多报价单的常态，缺证据不等于有问题。
        把它当不可用会拦掉绝大多数正常文档；把它当通过又会让缺口隐形。
        正确处置是**放行 + 强制可见**，由调用方展示 `reasons`。

        （注意与 CLAUDE.md §4 的 REVIEW 区分：那条讲的是**行级** pending /
        review_candidate 不得进入正式报价，这里是**份级**的证据完备度。）
        """
        return self.verdict != BLOCKED

    @property
    def clean(self) -> bool:
        """没有任何疑点。调用方要"只用完全干净的"时用这个。"""
        return self.verdict == OK

    def to_dict(self) -> dict:
        return {"submission_id": self.submission_id, "verdict": self.verdict,
                "eligible": self.eligible, "clean": self.clean, "stats": self.stats,
                "reasons": [r.to_dict() for r in self.reasons]}


def _worse(a: str, b: str) -> str:
    order = {OK: 0, REVIEW: 1, BLOCKED: 2}
    return a if order[a] >= order[b] else b


def evaluate_submission(db: Session, submission: BidSubmission) -> EligibilityVerdict:
    """按已持久化的证据判定一份报价能否进入正式比价。"""
    v = EligibilityVerdict(submission_id=submission.id, verdict=OK)

    def add(level: str, code: str, message: str, **evidence) -> None:
        v.verdict = _worse(v.verdict, level)
        v.reasons.append(Reason(code=code, message=message, evidence=evidence))

    # ① 生命周期状态
    if submission.status in INELIGIBLE_STATUSES:
        add(BLOCKED, "submission_status",
            f"submission 状态为 {submission.status}，不参与比价",
            status=submission.status)

    lines = db.scalars(
        select(BidQuoteLine).where(BidQuoteLine.submission_id == submission.id)
    ).all()
    v.stats["line_count"] = len(lines)
    if not lines:
        add(BLOCKED, "no_lines", "该 submission 没有任何报价行")
        return v

    # ② 声明总价闭环。**unknown 不是通过**——没有这个证据就是没有，
    #    而不是"校验过了"。之前 bid_matrix 把 unknown 当通过，等于默认放行。
    job = db.get(ExtractionJob, submission.job_id) if submission.job_id else None
    checksum = ((job.result or {}).get("_checksum") or {}) if job else {}
    cs_status = checksum.get("status", "unknown")
    v.stats["checksum_status"] = cs_status
    if cs_status == "fail":
        add(BLOCKED, "checksum_failed",
            f"明细合计与文件声明总价不符（差 {checksum.get('delta_pct')}%）",
            **checksum)
    elif cs_status != "pass":
        add(REVIEW, "checksum_unknown",
            "没有文件声明总价，无法闭环校验——不等于校验通过", **checksum)

    # ③ 结构缺陷行：列错位没有合法形态，一行即不可用
    shifted = [ln for ln in lines
               if COLUMN_SHIFT_FLAG in ((ln.extraction_meta or {}).get("validation_flags") or [])]
    v.stats["column_shift_rows"] = len(shifted)
    if shifted:
        add(BLOCKED, "column_shift",
            f"{len(shifted)} 行存在列错位，按列名取到的值不可信",
            rows=[ln.id for ln in shifted[:20]])

    # ④ 合价来源。missing = 原文该有金额却没读到且未经人工补写，
    #    not_quoted = 原文明确不报价（合法，只作统计）。
    by_source: dict[str, int] = {}
    for ln in lines:
        src = (ln.extraction_meta or {}).get("total_source") or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
    v.stats["total_source"] = by_source
    if by_source.get("missing"):
        add(BLOCKED, "missing_total",
            f"{by_source['missing']} 行原文无合价且未经人工确认",
            count=by_source["missing"])

    v.stats["not_quoted_rows"] = by_source.get("not_quoted", 0)
    return v


# ⑤ 价格覆盖率 / 算术错误率 / VAT / 集中度 / 声明总价对比 —— **有意不放在这里**。
#
# `routes/analysis.py` 的 match 门已有这几项，且处理了这里缺的语义：合计/小计行必须
# 排除在分母外（用 `_is_summary_row`），否则一份含合计行的正常报价会被算成 67% 覆盖率
# 而误拦——本模块第一版就踩了这个坑。
#
# 在这里再写一遍会得到**两套语义略有差异的覆盖率**，正是本模块要消除的问题。
# 所以分工是：
#   本模块  —— 下游此前**完全没有**的判据（状态 / 声明总价闭环 / 列错位 / 合价来源）
#   match 门 —— 已有且正确的统计类判据
# 待后续把 match 门那几项迁进来时，必须连同 summary-row 排除和既有响应结构一起搬，
# 不能只搬公式。**这是已知的、有意保留的分工，不是遗漏。**


def evaluate_many(db: Session, submissions) -> list[EligibilityVerdict]:
    return [evaluate_submission(db, s) for s in submissions]


def blocking_summary(verdicts: list[EligibilityVerdict]) -> list[dict]:
    """把不可用的判定整理成给调用方/前端的结构化说明。**不合并原因**——
    每份报价为什么不能用必须逐条可见，合并成一句话就没法处理了。"""
    return [v.to_dict() for v in verdicts if not v.eligible]
