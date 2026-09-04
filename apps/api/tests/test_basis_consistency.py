"""轮内口径一致性判定的契约测试（P1）。

用例直接取自 `docs/test2` 的真实材料（临港中科院 母线槽 / 潜水泵），不是编的：
第一轮四家中上海都安报「不含安装」827,034，其余三家「含安装」；四家铜价基准
各不相同（77540 / 76600 / 77470 / 77680 元/吨），二轮才统一到 73410。

这套测试要钉死的核心行为是：**未知 ≠ 一致**。模型抽到但没人确认的口径不能
被当成"大家都一样"放过去。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apps.api.models.bid_submission import BidSubmission
from apps.api.models.project import Project
from apps.api.models.submission_basis import (
    DIM_COMMODITY_BENCHMARK,
    DIM_DELIVERY_SCOPE,
    STATUS_CONFIRMED,
    STATUS_EXTRACTED,
    STATUS_EXTRACTION_FAILED,
    STATUS_NOT_PRESENT,
    SubmissionBasis,
)
from apps.api.services.matrix.basis_consistency import (
    VALUE_NOT_DECLARED,
    check_round_basis,
    upsert_basis,
)


def _project(db) -> Project:
    p = Project(name="临港中科院项目", code="LG-2026-003", status="active")
    db.add(p)
    db.flush()
    return p


def _submission(db, project_id: int, supplier_raw_name: str, batch: str) -> BidSubmission:
    sub = BidSubmission(
        job_id=f"job-{batch}",
        supplier_raw_name=supplier_raw_name,
        project_id=project_id,
        batch_id=batch,
        status="confirmed",
    )
    db.add(sub)
    db.flush()
    return sub


def _round_of(db, project_id: int, names: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, n in enumerate(names):
        s = _submission(db, project_id, n, f"b{project_id}-{i}-{n}")
        out.append((s.id, n))
    return out


# ── 交付范围：母线第一轮的真实反例 ──────────────────────────────────────────

def test_delivery_scope_conflict_blocks_comparison(db_session):
    """一家「不含安装」混在三家「含安装」里 → 不可比。

    这正是真实材料里发生过的事：按最低价排，都安 827,034 第一，而它便宜只是
    因为没含安装。
    """
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["上海都安实业", "江苏永旗电气", "上海塞克西德", "大航有能电气"])

    upsert_basis(db_session, subs[0][0], DIM_DELIVERY_SCOPE,
                 status=STATUS_CONFIRMED, value={"scope": "excl_installation"},
                 raw_text="不含安装", confirmed_by="admin")
    for sid, _ in subs[1:]:
        upsert_basis(db_session, sid, DIM_DELIVERY_SCOPE,
                     status=STATUS_CONFIRMED, value={"scope": "incl_installation"},
                     raw_text="含安装", confirmed_by="admin")

    rep = check_round_basis(db_session, subs, dims=(DIM_DELIVERY_SCOPE,))

    assert rep.comparable is False
    assert len(rep.conflicts) == 1
    conflict = rep.conflicts[0]
    assert conflict.dim == DIM_DELIVERY_SCOPE
    # 差异要能说清"谁跟谁不一样"，不是只说一句"不一致"
    assert len(conflict.values) == 2
    buckets = {k: sorted(v) for k, v in conflict.values.items()}
    assert ["上海都安实业"] in buckets.values()
    assert sorted(["江苏永旗电气", "上海塞克西德", "大航有能电气"]) in buckets.values()


def test_same_scope_is_comparable(db_session):
    """四家口径一致 → 可比（二轮把基准拉平后的目标状态）。"""
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["都安", "永旗", "塞克西德", "大航"])
    for sid, _ in subs:
        upsert_basis(db_session, sid, DIM_COMMODITY_BENCHMARK,
                     status=STATUS_CONFIRMED,
                     value={"material": "铜", "price": 73410, "unit": "元/吨"},
                     raw_text="铜价基准：73410元/吨", confirmed_by="admin")

    rep = check_round_basis(db_session, subs, dims=(DIM_COMMODITY_BENCHMARK,))
    assert rep.comparable is True
    assert rep.conflicts == []
    assert rep.unresolved == {}


def test_commodity_benchmark_conflict(db_session):
    """一轮四家各报各的铜价基准 → 不可比。"""
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["都安", "永旗", "塞克西德", "大航"])
    for (sid, _), price in zip(subs, [77540, 76600, 77470, 77680]):
        upsert_basis(db_session, sid, DIM_COMMODITY_BENCHMARK,
                     status=STATUS_CONFIRMED,
                     value={"material": "铜", "price": price, "unit": "元/吨"},
                     raw_text=f"铜价基准：{price}元/吨", confirmed_by="admin")

    rep = check_round_basis(db_session, subs, dims=(DIM_COMMODITY_BENCHMARK,))
    assert rep.comparable is False
    assert len(rep.conflicts[0].values) == 4  # 四家四个值


# ── 未知 ≠ 一致（用户 2026-09-03 决策 2 的附加约束）──────────────────────────

def test_extracted_but_unconfirmed_is_not_treated_as_agreement(db_session):
    """模型抽到、没人确认 → 未决，**不能**当成一致放过。

    这条是决策 2（直接上 LLM 抽取）的安全底线：模型抽错时，错误值不会自己
    变成"大家都一样"的依据。
    """
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["甲", "乙"])
    for sid, _ in subs:
        upsert_basis(db_session, sid, DIM_DELIVERY_SCOPE,
                     status=STATUS_EXTRACTED,   # 模型抽的，未确认
                     value={"scope": "incl_installation"}, raw_text="含安装",
                     extracted_by="qwen-vl:test")

    rep = check_round_basis(db_session, subs, dims=(DIM_DELIVERY_SCOPE,))

    # 值看起来一样，但都没确认——照样不可比
    assert rep.comparable is False
    assert rep.conflicts == []          # 不是"冲突"，是"未决"
    assert set(rep.unresolved) == {subs[0][0], subs[1][0]}


def test_missing_row_is_unresolved_not_agreement(db_session):
    """整条记录都没有 → 未决，不是"一致"。"""
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["甲", "乙"])
    rep = check_round_basis(db_session, subs, dims=(DIM_DELIVERY_SCOPE,))
    assert rep.comparable is False
    assert len(rep.unresolved) == 2


def test_extraction_failed_is_unresolved_not_not_present(db_session):
    """抽取失败 ≠ 原文里没有。前者要重试，后者是业务事实。"""
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["甲", "乙"])
    upsert_basis(db_session, subs[0][0], DIM_DELIVERY_SCOPE,
                 status=STATUS_EXTRACTION_FAILED, raw_text="", extracted_by="qwen-vl:test")
    upsert_basis(db_session, subs[1][0], DIM_DELIVERY_SCOPE,
                 status=STATUS_CONFIRMED, value={"scope": "incl_installation"},
                 raw_text="含安装", confirmed_by="admin")

    rep = check_round_basis(db_session, subs, dims=(DIM_DELIVERY_SCOPE,))
    assert rep.comparable is False
    assert subs[0][0] in rep.unresolved


def test_not_present_is_a_value_and_conflicts_with_a_declaration(db_session):
    """一家明说「不含安装」、另一家整份没提 → 不一致。

    没提不是"跟别人一样"：到底含不含谁也不知道，正是要拦的情况。
    """
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["甲", "乙"])
    upsert_basis(db_session, subs[0][0], DIM_DELIVERY_SCOPE,
                 status=STATUS_CONFIRMED, value={"scope": "excl_installation"},
                 raw_text="不含安装", confirmed_by="admin")
    upsert_basis(db_session, subs[1][0], DIM_DELIVERY_SCOPE,
                 status=STATUS_NOT_PRESENT, raw_text="", confirmed_by=None)

    rep = check_round_basis(db_session, subs, dims=(DIM_DELIVERY_SCOPE,))
    assert rep.comparable is False
    assert VALUE_NOT_DECLARED in rep.conflicts[0].values


# ── 其他契约 ────────────────────────────────────────────────────────────────

def test_single_submission_round_is_comparable(db_session):
    """只有一家报价时无横向比较可言，不该报不可比。"""
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["独苗"])
    assert check_round_basis(db_session, subs).comparable is True


def test_value_key_is_order_insensitive(db_session):
    """同一个付款条件、字典字面量顺序不同 → 必须算同一个值。

    用 str(dict) 做键会让这两家被判成不一致——比不判还糟。
    """
    from apps.api.models.submission_basis import DIM_PAYMENT_TERMS
    proj = _project(db_session)
    subs = _round_of(db_session, proj.id, ["甲", "乙"])
    upsert_basis(db_session, subs[0][0], DIM_PAYMENT_TERMS, status=STATUS_CONFIRMED,
                 value={"advance_pct": 70, "retention_pct": 5}, confirmed_by="admin")
    upsert_basis(db_session, subs[1][0], DIM_PAYMENT_TERMS, status=STATUS_CONFIRMED,
                 value={"retention_pct": 5, "advance_pct": 70}, confirmed_by="admin")

    rep = check_round_basis(db_session, subs, dims=(DIM_PAYMENT_TERMS,))
    assert rep.comparable is True


def test_upsert_updates_in_place(db_session):
    """改值是 UPDATE，不插第二行（唯一约束）。"""
    proj = _project(db_session)
    sub = _submission(db_session, proj.id, "甲", "b-upsert")
    upsert_basis(db_session, sub.id, DIM_DELIVERY_SCOPE, status=STATUS_EXTRACTED,
                 value={"scope": "incl_installation"}, raw_text="含安装",
                 extracted_by="qwen-vl:test")
    upsert_basis(db_session, sub.id, DIM_DELIVERY_SCOPE, status=STATUS_CONFIRMED,
                 value={"scope": "excl_installation"}, raw_text="不含安装",
                 confirmed_by="admin")

    rows = db_session.scalars(
        select(SubmissionBasis).where(SubmissionBasis.submission_id == sub.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].status == STATUS_CONFIRMED
    assert rows[0].value == {"scope": "excl_installation"}
    assert rows[0].confirmed_by == "admin"
    # extracted_by 不被确认动作抹掉——要能回答"这个候选原本是谁抽的"
    assert rows[0].extracted_by == "qwen-vl:test"


def test_unknown_dimension_rejected(db_session):
    proj = _project(db_session)
    sub = _submission(db_session, proj.id, "甲", "b-baddim")
    with pytest.raises(ValueError):
        upsert_basis(db_session, sub.id, "made_up_dim", status=STATUS_CONFIRMED)
