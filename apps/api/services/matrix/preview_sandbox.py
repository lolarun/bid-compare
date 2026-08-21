"""preview_sandbox.py — design/31 §4.1：一个"写完必然回滚"的会话。

预览比价要跑的是**官方那条链路本身**（校对入库 → 对齐 → 矩阵），不是另写
一份只读实现——两份实现算"同一个比价"迟早会算出两个数，CLAUDE.md 的
"同一个业务结果"就是为了挡这个。可官方链路天生要写库（`confirm_batch` /
`anchor_match` / `finalize_alignment` 各自都会 `db.commit()`），而预览
**一个字节都不许落库**。

## 为什么不是简单的"最后 rollback 一下"

链路内部真的调了 `db.commit()`。普通会话上 commit 之后再 rollback 什么也
撤不回来。所以这里用 SQLAlchemy 2.0 的 **join an external transaction**：
外层连接自己开一个事务，会话以 `join_transaction_mode="create_savepoint"`
挂上去——链路内部的 `commit()` 变成"释放一个 SAVEPOINT"，外层事务始终没
提交，退出时整体 rollback。这是 SQLAlchemy 文档里的既定用法，不是绕过
ORM 的偏方。

## SQLite 必须额外做一件事

pysqlite 默认会替我们隐式管理 BEGIN，结果是 **SAVEPOINT 之外的外层事务
根本没真正开始**，退出时 rollback 什么也撤不回来。这不是推测，是实测：
同一段代码，不打这个补丁"回滚后"仍然数得到刚写的行（见
`test_preview_sandbox.py`，正是它先抓到的）。SQLAlchemy 文档给的解法是把
隐式 BEGIN 关掉、由自己在 "begin" 事件里发 `BEGIN`。

这个补丁**只打在沙箱自己的 engine 上**，不动 `database.engine`：那是全应用
每一个事务都要走的东西，为了一个预览功能改掉它的事务语义，代价和收益完全
不成比例。代价是沙箱多一个连接池——SQLite 上无所谓。

## 边界（必须知道，别以为万无一失）

1. **只管住经由本会话的写。** 如果被调用的代码自己 `SessionLocal()` 开一个
   新会话，那条会话连的是另一个连接，不在这个事务里，会真的落库。已核查：
   confirm/对齐/矩阵这条链路上没有这种代码，唯一自开会话的是
   `document_ingestion.py`（后台识别任务），不在预览路径上。**以后往预览
   链路里加东西时要重新核这一条。**
2. **SQLite 写锁。** 外层事务一开就持有写锁，期间别的写入会等。预览要跑
   完整条对齐+矩阵，比单文件 dry-run 长得多——上线前必须实测这段时长，
   不能拿单文件的耗时外推（design/31 §4.1 已记为待办）。
3. **不是权限闸。** 它保证"不落库"，不保证"不该看的看不到"。业务上哪些
   数据可以进预览，由调用方按 CLAUDE.md 的分层规则决定，不归这里。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_lock = threading.Lock()
_sandbox_engine: Engine | None = None
_sandbox_url: str | None = None


def _build_sandbox_engine(url: str, connect_args: dict) -> Engine:
    eng = create_engine(url, connect_args=connect_args)
    if eng.dialect.name == "sqlite":
        @event.listens_for(eng, "connect")
        def _disable_implicit_begin(dbapi_conn, _record):        # noqa: ANN001
            # pysqlite 的隐式 BEGIN 会让外层事务名存实亡（见模块文档）。
            dbapi_conn.isolation_level = None

        @event.listens_for(eng, "begin")
        def _emit_begin(conn):                                    # noqa: ANN001
            conn.exec_driver_sql("BEGIN")
    return eng


def _get_sandbox_engine() -> Engine:
    """沙箱专用 engine（懒建、按 URL 缓存）。

    按 URL 缓存而不是建一次就固定：测试的 `temp_db` fixture 每个用例换一个
    库并 monkeypatch `database.engine`，固定住会让沙箱连到上一个测试的库。
    """
    global _sandbox_engine, _sandbox_url
    from apps.api.core import database as db_mod

    url = str(db_mod.engine.url)
    with _lock:
        if _sandbox_engine is None or _sandbox_url != url:
            if _sandbox_engine is not None:
                _sandbox_engine.dispose()
            _sandbox_engine = _build_sandbox_engine(
                url, dict(db_mod.engine.dialect.create_connect_args(db_mod.engine.url)[1]))
            _sandbox_url = url
        return _sandbox_engine


@contextmanager
def preview_sandbox() -> Iterator[Session]:
    """产出一个会话，块内的一切写入在退出时全部撤销——正常退出也撤销。

    没有"成功就提交"这条路径：这个上下文管理器的**唯一**语义就是不落库。
    想落库的调用方请走官方路径，不要给这里加一个 commit 开关——那个开关
    存在的第一天就会有人打开它。
    """
    connection = _get_sandbox_engine().connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint",
                      autoflush=False)
    try:
        yield session
    finally:
        try:
            session.close()
        finally:
            if outer.is_active:
                outer.rollback()
            connection.close()
            log.debug("preview_sandbox: rolled back, nothing persisted")
