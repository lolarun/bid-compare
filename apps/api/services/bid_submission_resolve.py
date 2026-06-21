"""Shared utility: resolve the active BidSubmission set for a project+category.

All endpoints that need to identify which BidSubmission rows to use
(match, anchor-review, residue, LLM-fill) MUST call this function instead
of writing their own submission queries, so they all operate on the same batch.

Return type: dict[submission_id → BidSubmission]  (keyed by BidSubmission.id)
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from apps.api.models.bid_submission import BidQuoteLine, BidSubmission


def resolve_active_submissions(
    db: Session,
    project_id: int,
    category: str,
    supplier_ids: list[int] | None = None,
    submission_ids: list[int] | None = None,
) -> dict[int, BidSubmission]:
    """Return {submission_id → BidSubmission} for active submissions.

    Priority rules (互斥，非 OR):
    - submission_ids 非空 → 唯一权威集合，完全忽略 supplier_ids，禁止 union。
    - submission_ids 为空 → 以 supplier_ids 查询（旧链路兼容）。
    - 两者均为空 → 返回项目+品类下全部 active submissions。

    Status filter: 始终排除 rejected AND superseded。
    BQL filter: 必须在 category 下存在至少一行 BidQuoteLine。
    """
    q = (
        db.query(BidSubmission)
        .join(BidQuoteLine, BidSubmission.id == BidQuoteLine.submission_id)
        .filter(
            BidSubmission.project_id == project_id,
            BidSubmission.status.notin_(["rejected", "superseded"]),
            BidQuoteLine.category == category,
        )
        .distinct()
    )

    if submission_ids:
        # submission_ids 是调用方明确指定的权威集合，不允许 supplier_ids 扩展
        q = q.filter(BidSubmission.id.in_(submission_ids))
    elif supplier_ids:
        q = q.filter(BidSubmission.supplier_id.in_(supplier_ids))
    # 两者均为空：不附加额外过滤（返回全部 active）

    result: dict[int, BidSubmission] = {}
    for sub in q.order_by(BidSubmission.id.asc()).all():
        if sub.id not in result:
            result[sub.id] = sub
    return result
