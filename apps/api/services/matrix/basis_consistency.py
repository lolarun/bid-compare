"""轮内口径一致性判定 —— P1，见 `.claude/plans/comparability-basis-dimensions.md` D3。

一句话：同一轮里各家报价如果在**交付范围 / 原材料价格基准 / 付款条件**上取值不同，
它们的总价就不能直接比，本轮不出排名，改出差异说明。

**这里没有模型调用。** 值从哪来是识别层的事；本模块只做纯等值比较——口径判定
必须是确定性的，不然"能不能比"这件事本身就会随模型漂移。

三条硬规则：

1. **只吃 `confirmed` 的值。** 模型抽出来但没人确认的（`extracted`）不参与判定，
   也**不当作"一致"放过**——未知 ≠ 一致（用户 2026-09-03 决策 2 的附加约束）。
2. **`not_present` 是一个取值，不是缺失。** 一家明说「不含安装」、另一家整份没提
   交付范围，这两者就是不一致：后者到底含不含谁也不知道，正是要拦的情况。
3. **不折算、不排序、不给建议。** 只回答"是否一致"和"差在哪"，谁更优是评标小组的事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models.submission_basis import (
    DIMENSIONS,
    STATUS_CONFIRMED,
    STATUS_EXTRACTION_FAILED,
    STATUS_NOT_PRESENT,
    SubmissionBasis,
)

#: 值不可比但"已知没有声明"时，在差异里显示的占位。它是一个**取值**，
#: 跟"还没确认"（unresolved）是两回事，不能合并。
VALUE_NOT_DECLARED = "__not_declared__"


@dataclass
class DimensionConflict:
    """一个维度上的取值分布。`values` 至少两个不同键才算冲突。"""

    dim: str
    #: {归一值的稳定字符串 → [supplier_name, ...]}
    values: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"dim": self.dim, "values": self.values}


@dataclass
class RoundBasisReport:
    """一轮的口径体检结果。

    `comparable` 为 False 时调用方**不得**出总价排名；出的话就是把不可比的数
    摆成可比的（design §1.1 的真实反例：827,034「不含安装」混在三家「含安装」里）。
    """

    comparable: bool
    conflicts: list[DimensionConflict] = field(default_factory=list)
    #: 还没确认口径的报价（submission_id → supplier_name）。未知 ≠ 一致，
    #: 有未决项时 comparable 一律 False。
    unresolved: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "comparable": self.comparable,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "unresolved": [
                {"submission_id": sid, "supplier_name": name}
                for sid, name in sorted(self.unresolved.items())
            ],
        }


def _value_key(row: SubmissionBasis) -> str:
    """把一个口径取值压成可比较的稳定字符串。

    用排序后的 JSON 而不是 `str(dict)`：字典字面量的顺序会让同一个值算出两个键，
    于是两家其实一样的付款条件被判成不一致——比不判还糟。
    """
    if row.status == STATUS_NOT_PRESENT:
        return VALUE_NOT_DECLARED
    return json.dumps(row.value, sort_keys=True, ensure_ascii=False)


def check_round_basis(
    db: Session,
    submissions: list[tuple[int, str]],
    dims: tuple[str, ...] = DIMENSIONS,
) -> RoundBasisReport:
    """体检一轮的口径一致性。

    `submissions` 是 [(submission_id, supplier_name), ...]——**列身份是
    submission_id 不是 supplier_id**（CLAUDE.md §4：同一供应商可以有多份报价）。
    supplier_name 只用于把差异说给人听。

    少于两份报价时无所谓可比不可比，直接返回 comparable=True（没有横向比较发生）。
    """
    if len(submissions) < 2:
        return RoundBasisReport(comparable=True)

    sub_ids = [sid for sid, _ in submissions]
    names = dict(submissions)

    rows = db.scalars(
        select(SubmissionBasis).where(
            SubmissionBasis.submission_id.in_(sub_ids),
            SubmissionBasis.dim.in_(dims),
        )
    ).all()

    # (submission_id, dim) → row
    by_sub_dim: dict[tuple[int, str], SubmissionBasis] = {
        (r.submission_id, r.dim): r for r in rows
    }

    unresolved: dict[int, str] = {}
    conflicts: list[DimensionConflict] = []

    for dim in dims:
        buckets: dict[str, list[str]] = {}
        for sid in sub_ids:
            row = by_sub_dim.get((sid, dim))
            # 没有记录 / 抽取失败 / 抽到但没确认 → 未决。三者都不参与分桶，
            # 但都会让整轮 comparable=False（未知 ≠ 一致）。
            if row is None or row.status == STATUS_EXTRACTION_FAILED:
                unresolved[sid] = names[sid]
                continue
            if row.status not in (STATUS_CONFIRMED, STATUS_NOT_PRESENT):
                unresolved[sid] = names[sid]
                continue
            buckets.setdefault(_value_key(row), []).append(names[sid])

        if len(buckets) > 1:
            conflicts.append(DimensionConflict(dim=dim, values=buckets))

    return RoundBasisReport(
        comparable=not conflicts and not unresolved,
        conflicts=conflicts,
        unresolved=unresolved,
    )


def upsert_basis(
    db: Session,
    submission_id: int,
    dim: str,
    *,
    status: str,
    value: Any = None,
    raw_text: str = "",
    source_ref: dict | None = None,
    extracted_by: str = "",
    confirmed_by: str | None = None,
) -> SubmissionBasis:
    """写入/更新一份报价的某个口径取值。不提交——调用方持有事务边界。

    改值是 UPDATE 而不是插第二行（唯一约束 uq_submission_basis_dim）：历史留在
    操作日志里，这张表只保当前值。
    """
    from datetime import UTC, datetime

    if dim not in DIMENSIONS:
        raise ValueError(f"Unknown basis dimension: {dim!r}")

    row = db.scalar(
        select(SubmissionBasis).where(
            SubmissionBasis.submission_id == submission_id,
            SubmissionBasis.dim == dim,
        )
    )
    if row is None:
        row = SubmissionBasis(submission_id=submission_id, dim=dim)
        db.add(row)

    row.status = status
    row.value = value
    row.raw_text = raw_text or ""
    row.source_ref = source_ref
    if extracted_by:
        row.extracted_by = extracted_by
    if status == STATUS_CONFIRMED:
        row.confirmed_by = confirmed_by
        row.confirmed_at = datetime.now(UTC)
    db.flush()
    return row
