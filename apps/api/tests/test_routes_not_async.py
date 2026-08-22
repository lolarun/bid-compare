"""路由里不许把阻塞重活写在 `async def` 里（2026-08-22）。

## 为什么值得一条专门的回归

FastAPI 对两种路由的调度**完全不同**：`def` 路由丢进线程池，`async def`
路由直接在事件循环上跑。所以在 `async def` 里做同步阻塞调用，卡住的不是
"这一个请求"，是**整个服务器**——那段时间里任何请求都不会被处理，连
`/api/health` 都不会。

这不是理论。本项目实测撞了四次，每次都被误诊成别的东西：

| 现象 | 当时的判断 | 真因 |
|---|---|---|
| 泰科龙分类"接口异常" | 客户端 30s 超时太紧 | 事件循环被视觉调用占住 |
| 上海绵存"轮询超时" | GIL / pdfium 锁竞争 | 同上 |
| 泰科龙上传 `timeout of 60000ms exceeded` | 文件大 | `create_job` 在事件循环里写盘+抢 SQLite 写锁 |
| 同一时刻 `PUT /api/projects` 也超时 | 以为是另一个 bug | 同一个事件循环被占住 |

前两次我把它归因成"GIL 和 pdfium 锁竞争"——那个说法**不对**：锁竞争只会让
请求变慢，不会让请求根本得不到处理。真正的机制是事件循环被独占。

## 判据

对 `apps/api/routes/*.py` 做 AST 扫描：`async def` 路由函数体里不得出现
已知的同步阻塞调用。名单是白名单式的（列出已知的重活），不求穷尽——它的
作用是拦住**回归**，不是证明不存在其它阻塞。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROUTES_DIR = pathlib.Path(__file__).resolve().parents[1] / "routes"

#: 已知的同步阻塞调用。新增重活时往这里加一条，比事后查四次超时便宜得多。
BLOCKING_CALLS = {
    "create_job",                  # SHA256 + 写盘 + SQLite 写锁
    "classify_tier0",              # pdfium 渲染
    "classify_pdf_for_dispatch",   # 真实视觉 HTTP，实测 6.5-9s
    "compose_summary",             # 纯文本 LLM 调用
    "build_preview_matrix",        # 沙箱内整条链路
    "import_and_match",            # 对齐，可能走 embedding HTTP
    "confirm_batch",
    "build_anchor_matrix",
    "recognize_quote_paddle",
    "submit_and_parse",
    "render_pages",
    "get_bid_matrix_for_export",
}


def _async_routes_with_blocking_calls() -> list[tuple[str, str, int, list[str]]]:
    found: list[tuple[str, str, int, list[str]]] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            names: set[str] = set()
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                if isinstance(call.func, ast.Name):
                    names.add(call.func.id)
                elif isinstance(call.func, ast.Attribute):
                    names.add(call.func.attr)
            hits = sorted(names & BLOCKING_CALLS)
            if hits:
                found.append((path.name, node.name, node.lineno, hits))
    return found


def test_no_blocking_call_inside_an_async_route():
    offenders = _async_routes_with_blocking_calls()
    assert not offenders, (
        "以下 async def 路由里有同步阻塞调用，会卡死整个事件循环（不只是这一个"
        "请求）。改成 `def`，FastAPI 会放进线程池；文件读取相应从 "
        "`await file.read()` 改成 `file.file.read()`：\n"
        + "\n".join(f"  {f}:{ln} async def {fn} -> {hits}" for f, fn, ln, hits in offenders)
    )


@pytest.mark.parametrize("module_name,func_name", [
    ("intake", "upload_document"),
    ("intake", "classify_tier0_upload"),
    ("intake", "summarize_facts"),
    ("analysis", "tender_list_match"),
])
def test_known_heavy_routes_stay_sync(module_name: str, func_name: str):
    """这四个是实测撞过超时的具体路由，单独钉住。

    上面那条 AST 扫描依赖 `BLOCKING_CALLS` 名单；万一有人重构时把调用换了个
    名字，扫描会漏，这四条不会。
    """
    import importlib
    import inspect

    mod = importlib.import_module(f"apps.api.routes.{module_name}")
    fn = getattr(mod, func_name)
    assert not inspect.iscoroutinefunction(fn), (
        f"{module_name}.{func_name} 又变回 async def 了——它的函数体是阻塞的，"
        f"会占住事件循环让整个服务停止响应。")
