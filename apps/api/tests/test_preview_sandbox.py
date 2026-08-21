"""design/31 §4.1：证明预览沙箱真的不落库。

这份测试的立场是**不信任**：`preview_sandbox` 的全部价值就是"写不进去"，
所以断言方式是从沙箱**外面另开一个会话**去数行数，而不是问沙箱自己
"你提交了吗"。内部 `commit()` 之后仍然数不到，才算数。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from apps.api.models import Project, Supplier
from apps.api.services.matrix.preview_sandbox import preview_sandbox


def _count(SessionLocal, model) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_writes_inside_are_invisible_outside_even_after_commit(temp_db):
    """链路内部真的会 `db.commit()`（confirm_batch/anchor_match 都会）。
    沙箱的关键就是让那个 commit 只释放 SAVEPOINT，外层事务照样不提交。"""
    _engine, SessionLocal = temp_db
    before = _count(SessionLocal, Project)

    with preview_sandbox() as db:
        db.add(Project(name="预览沙箱项目", code="PV-1"))
        db.commit()                      # 模拟链路内部的真实提交
        # 沙箱内看得到——否则后续的对齐/矩阵读不到自己刚写的数据
        assert db.scalar(select(func.count()).select_from(Project)) == before + 1
        # 沙箱外此刻数不到
        assert _count(SessionLocal, Project) == before

    assert _count(SessionLocal, Project) == before


def test_multiple_commits_all_roll_back(temp_db):
    """预览要连着跑 N 家的入库 + 一次对齐，中间会 commit 很多次。"""
    _engine, SessionLocal = temp_db
    before_p, before_s = _count(SessionLocal, Project), _count(SessionLocal, Supplier)

    with preview_sandbox() as db:
        for i in range(3):
            db.add(Project(name=f"预览项目{i}", code=f"PV-{i}"))
            db.commit()
        db.add(Supplier(name="预览供应商"))
        db.commit()

    assert _count(SessionLocal, Project) == before_p
    assert _count(SessionLocal, Supplier) == before_s


def test_exception_inside_still_rolls_back_and_propagates(temp_db):
    _engine, SessionLocal = temp_db
    before = _count(SessionLocal, Project)

    with pytest.raises(RuntimeError, match="boom"):
        with preview_sandbox() as db:
            db.add(Project(name="炸之前", code="PV-X"))
            db.commit()
            raise RuntimeError("boom")

    assert _count(SessionLocal, Project) == before


def test_sandbox_reads_existing_data(temp_db):
    """预览要读真实的招标清单/已入库报价——沙箱不是空库，是真库的可写副本。"""
    _engine, SessionLocal = temp_db
    with SessionLocal() as s:
        s.add(Project(name="真实项目", code="REAL-1"))
        s.commit()

    with preview_sandbox() as db:
        found = db.scalar(select(Project).where(Project.code == "REAL-1"))
        assert found is not None and found.name == "真实项目"

    # 读不改写：真实数据原封不动
    assert _count(SessionLocal, Project) == 1


def test_no_commit_escape_hatch_exists():
    """守住 §4.1 的那句话：不给这个上下文管理器加"成功就提交"的开关。
    开关存在的第一天就会有人打开它，那时预览就不再是预览了。"""
    import inspect

    from apps.api.services.matrix import preview_sandbox as mod

    sig = inspect.signature(mod.preview_sandbox)
    assert not sig.parameters, f"preview_sandbox 不应有参数，实际：{list(sig.parameters)}"
    src = inspect.getsource(mod.preview_sandbox)
    assert "outer.commit()" not in src
