"""design/31 §4.1 待办：预览沙箱持 SQLite 写锁多久，别的写入被挡多久。

设计文档把这条列为"上线前必须实测，不能拿单文件 dry-run 的耗时外推"。
这份测试就是那次实测，并且把结论钉成回归。

## 为什么这个数有一条硬线

SQLite 同一时刻只允许一个写事务。预览整条链路都跑在一个写事务里，期间任何
要写库的请求都得排队。排队本身没问题——**问题是排不过就直接报错**：
pysqlite 给连接的默认 `timeout` 是 **5 秒**（`sqlite3.connect(timeout=5.0)`，
本项目没有覆盖过），超过就抛 `sqlite3.OperationalError: database is locked`，
用户看到的是一次莫名其妙的保存失败。

所以 5 秒不是性能目标，是**正确性边界**：预览持锁一旦逼近它，并发的
"确认入库""改项目名"就会开始随机失败。`LOCK_BUDGET_S` 取 3 秒，留出余量。

## 量的是两个不同的东西，别混

1. **沙箱持锁多久** —— 预览链路的墙钟时间，随语料规模涨。
2. **别的写入者等多久、成不成功** —— 用户能感觉到的伤害。

## 踩过的坑（留着，别再踩）

第一版让沙箱在**持锁状态下** `wait()` 等写入者完成，而写入者在等锁——
自己写出了死锁，测出来的 `database is locked` 是测试的 bug 不是系统的。
测并发等待时，持锁方必须自己按时释放，不能等对方。
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import func, select, text

from apps.api.models import Project
from apps.api.services.matrix.preview_sandbox import preview_sandbox
from apps.api.services.matrix.preview_service import build_preview_matrix
from apps.api.tests.test_compare_integration import compare_client  # noqa: F401
from apps.api.tests.test_preview_service import _setup_unconfirmed  # noqa: F401

#: 沙箱允许持锁的上限（秒）。见模块文档：pysqlite 默认 5s 后抛
#: `database is locked`，这里留余量取 3s。
LOCK_BUDGET_S = 3.0

#: 金桥阀门那批真实语料的形状。锁时长跟写入量直接相关，拿 2 行的用例量出来
#: 的数字对上线没有参考价值。
ANCHORS, SUPPLIERS = 89, 3


def _seed(db, n: int) -> None:
    for i in range(n):
        db.add(Project(name=f"锁测项目-{i}", code=f"LOCK-{i}"))
        if i % 20 == 0:
            db.flush()


def test_hold_time_at_real_corpus_write_volume(temp_db):
    """89×3 条写入 + 回滚，沙箱持锁多久。"""
    _engine, _SessionLocal = temp_db
    t0 = time.perf_counter()
    with preview_sandbox() as db:
        _seed(db, ANCHORS * SUPPLIERS)
        db.commit()
    held = time.perf_counter() - t0
    print(f"\n[实测] 写入量 {ANCHORS * SUPPLIERS} 行时沙箱持锁 {held:.3f}s")
    assert held < LOCK_BUDGET_S, (
        f"沙箱持锁 {held:.2f}s，逼近 pysqlite 的 5s 默认超时——"
        f"并发写入会开始随机报 database is locked")


def test_hold_time_of_the_real_preview_chain(compare_client, temp_db):
    """真实链路（confirm → 对齐 → 矩阵）跑一遍的持锁时长。

    规模由集成夹具决定（2 锚点 × 2 家），比生产小。它测的是**链路固定开销**；
    随规模增长的那部分由上一条用例覆盖。两条都远低于预算才算过。
    """
    _engine, _SessionLocal = temp_db
    state = _setup_unconfirmed(compare_client)
    t0 = time.perf_counter()
    build_preview_matrix(state["project_id"], "阀门", state["confirmations"])
    held = time.perf_counter() - t0
    print(f"\n[实测] 真实预览链路（2 锚点 × 2 家）持锁 {held:.3f}s")
    assert held < LOCK_BUDGET_S


def test_concurrent_writer_waits_then_succeeds(temp_db):
    """预览期间另一个请求写库：会等，但必须等得到、写得进。

    断言的不是"不被挡"（写锁互斥，挡是必然的），而是"挡完能成功"。
    沉默失败才是不可接受的。
    """
    _engine, SessionLocal = temp_db
    waited: list[float] = []
    errors: list[str] = []
    sandbox_open = threading.Event()

    def writer():
        sandbox_open.wait(timeout=10)
        t0 = time.perf_counter()
        try:
            with SessionLocal() as s:
                s.add(Project(name="并发写入者", code="CONCURRENT-1"))
                s.commit()
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}"[:120])
        waited.append(time.perf_counter() - t0)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    # 关键：沙箱**不等**写入者，自己按时释放锁。等对方就是死锁（见模块文档）。
    with preview_sandbox() as db:
        _seed(db, ANCHORS * SUPPLIERS)
        db.commit()
        sandbox_open.set()
        time.sleep(0.5)
    t.join(timeout=20)

    print(f"\n[实测] 并发写入者等待 {waited[0]:.3f}s，结果：{errors or '成功'}")
    assert not errors, f"并发写入者没被挡住而是失败了：{errors}"

    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Project)
                        .where(Project.code == "CONCURRENT-1")) == 1


def test_rollback_leaves_concurrent_writes_intact(temp_db):
    """沙箱回滚只回滚自己那一份，别人的写入必须留下。"""
    _engine, SessionLocal = temp_db
    with SessionLocal() as s:
        s.add(Project(name="先存在的", code="PRE-1"))
        s.commit()

    with preview_sandbox() as db:
        _seed(db, 10)
        db.commit()

    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Project)) == 1
        assert s.scalar(select(Project).where(Project.code == "PRE-1")) is not None


def test_pysqlite_default_timeout_is_what_we_think_it_is(temp_db):
    """这一条守的是上面所有预算的**前提**。

    `LOCK_BUDGET_S` 之所以取 3 秒，唯一理由是 pysqlite 的默认 5 秒。哪天有人
    在 database.py 里调了 busy_timeout（或换了后端），这个前提就变了，预算
    也该跟着重算——这里会先红，逼人回来看这个文件。
    """
    _engine, SessionLocal = temp_db
    with SessionLocal() as s:
        busy_ms = s.execute(text("PRAGMA busy_timeout")).scalar()
    assert busy_ms == 5000, (
        f"busy_timeout 变成了 {busy_ms}ms（原本 5000）。"
        f"LOCK_BUDGET_S={LOCK_BUDGET_S}s 是按 5s 推出来的，请重新评估。")


def test_slow_io_inside_the_sandbox_breaks_concurrent_writes(temp_db):
    """把 5 秒那条线钉死：沙箱里只要有一段慢 I/O，并发写入就会**失败**。

    这不是假设。`import_and_match` 在顺序直连门不成立时会走 embedding 分支
    （`anchor_match._embed_client()` → 真实 HTTP），而它整段跑在沙箱的写事务
    里——网络往返多久，SQLite 写锁就被占多久。89 锚点 + 3×89 报价行要嵌入
    ~350 段文本，分批往返，秒级完全可能。

    这条用例用一段 6 秒的 sleep 代替那次网络调用，证明后果是**真的会报错**，
    不是"慢一点"：并发写入者拿到 `database is locked`。

    上面几条实测（持锁 ~50ms）之所以安全，前提是链路里**没有**慢 I/O。
    这条用例守的就是那个前提的反面，别把"实测 50ms"误读成"预览一定安全"。
    """
    _engine, SessionLocal = temp_db
    errors: list[str] = []
    sandbox_open = threading.Event()

    def writer():
        sandbox_open.wait(timeout=10)
        try:
            with SessionLocal() as s:
                s.add(Project(name="慢IO期间的写入者", code="SLOW-IO-1"))
                s.commit()
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}"[:80])

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    with preview_sandbox() as db:
        _seed(db, 10)
        db.commit()
        sandbox_open.set()
        time.sleep(6)          # ← 代替一次秒级网络往返，超过 pysqlite 的 5s
    t.join(timeout=20)

    print(f"\n[实测] 沙箱内持锁 6s 期间的并发写入：{errors or '成功'}")
    assert errors and "locked" in errors[0].lower(), (
        "预期并发写入被 database is locked 打断。如果这条不再成立，说明"
        "busy_timeout 或后端变了——LOCK_BUDGET_S 的推导要重来。")
