"""paddle_ocr.py — PaddleOCR-VL (百度智能云) 提交/轮询 provider（design/26 §5，P1）。

生产版 `scripts/try_paddleocr_vl.py` 探索脚本的提交/轮询逻辑，供
`apps.api.intelligence.paddle_vl.recognize_quote_paddle` 的 `submit_and_parse`
参数注入——两边保持职责分离：本模块只管"怎么跟百度云对话拿到结构化 JSON"，
`paddle_vl.py` 只管"结构化 JSON 怎么变成 CSV/ExtractionDraft"，可测试性跟
`vl_quote.py` 的 `VLCall` 注入是同一个道理（`.claude/rules/recognition.md`）。

跟脚本版的三点差异：
1. 重试/退避参数（`_MAX_RETRIES=5`、指数退避+抖动）沿用 `dashscope_ocr.py`
   同一组已验证过的数值，但是**本模块内独立实现**，不是从那边 import——
   `dashscope_ocr.py` 属于 qwen 链路，design/26 P4 要整体删除，import 它等于
   让本模块依赖一个即将消失的东西；重复这几行代码换来的是两条 provider
   互不牵连，qwen 删除时这里不需要跟着动一个字。
2. 每次提交/查询遇到非 200 的业务错误码都记录**完整响应体**，不只是失败就
   抛异常——design/26 P2 报预算前要核实有没有撞到限流/配额，这层日志是唯一
   的证据来源（脚本版探索阶段只把错误打印到终端，不落 log，事后查不到）。
3. 返回值是解析结果 dict 本身（`paddle_vl.build_quote_csv` 的输入契约），
   不落盘 markdown/json 副产物——那是脚本版为了人工核对留的，生产路径不需要。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from apps.api.core.config import get_settings
from apps.api.intelligence.base import ProviderError

log = logging.getLogger(__name__)

# 跟 dashscope_ocr.py 取同一套数值（独立定义，不 import——见模块文档差异 1），
# 不为 Paddle 另起一套没有依据的参数，沿用同一份"网络层容错够用多少次/退避
# 多久"的既有结论。
_MAX_RETRIES = 5
_RETRY_BASE = 2          # 指数退避基数：2, 4, 8, 16, 32（叠加抖动）
_RETRY_MAX = 30          # 封顶 30s

_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_TASK_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task"
_QUERY_URL = f"{_TASK_URL}/query"

# 轮询间隔与总超时——跟 try_paddleocr_vl.py 探索期实测一致（该脚本对 7 份真实
# 投标文件的实测耗时全部在 900s 内完成，含最大的一份 53 页文档）。
_POLL_INTERVAL_S = 5
_POLL_TIMEOUT_S = 900


def _retry_wait(attempt: int) -> float:
    """指数退避 + 全量抖动：min(base * 2^attempt, max) + uniform(0, 1)。"""
    return min(_RETRY_BASE * (2 ** attempt), _RETRY_MAX) + random.uniform(0, 1)


def _post_json(url: str, form: dict[str, str], *, timeout: int = 120,
               op: str = "call") -> dict:
    """带重试的表单 POST。两层失败都重试到 `_MAX_RETRIES`：
    - 网络层（连接失败/超时/响应体解析失败）——瞬时故障，退避后重试。
    - 业务层（HTTP 200 但 `error_code != 0`）——保守当成可能限流/配额瞬时问题
      处理，同样退避重试；**每次都记录完整响应体**，不管最终是否重试成功——
      这是 P2 判断"有没有撞到限流"唯一的证据来源，不能只在最终失败时才记。
    两层都耗尽重试后统一抛 `ProviderError`。
    """
    req_data = urlencode(form).encode("utf-8")
    last_error: str = ""
    for attempt in range(_MAX_RETRIES):
        try:
            req = Request(url, data=req_data,
                         headers={"Content-Type": "application/x-www-form-urlencoded"},
                         method="POST")
            with urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Paddle %s 第 %d/%d 次网络失败：%s", op, attempt + 1, _MAX_RETRIES, last_error)
        else:
            error_code = result.get("error_code")
            if error_code in (0, None):
                return result
            last_error = f"error_code={error_code} error_msg={result.get('error_msg')} body={result}"
            log.warning("Paddle %s 第 %d/%d 次业务错误：%s", op, attempt + 1, _MAX_RETRIES, last_error)
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_retry_wait(attempt))
    raise ProviderError(f"Paddle {op} 重试 {_MAX_RETRIES} 次仍失败：{last_error}")


def _download_json(url: str, *, timeout: int = 120) -> dict:
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _access_token(api_key: str, secret_key: str) -> str:
    result = _post_json(_TOKEN_URL, {
        "grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key,
    }, op="获取 access_token")
    token = str(result.get("access_token") or "")
    if not token:
        raise ProviderError(f"Paddle access_token 响应缺字段：{result}")
    return token


def submit_and_parse(file_path: str, *, merge_tables: bool = False,
                     recognize_seal: bool = True, page_count: int | None = None,
                     progress_cb=None) -> dict:
    """整份 PDF → 提交 → 轮询 → 下载结构化解析结果。

    `merge_tables=False`（**2026-08-22 由 True 改过来，7 份报价件 + 2 份招标件实测**）：
    开着的时候 Paddle 把跨页续表的**整段行塞进 begin 那一页的 table 对象**，
    inner/end 页只留一个空壳——行不丢、顺序不乱，但续页上的行**全部继承 begin
    页的页码**，`source_ref` 从此说的是错页（泰科龙 19/89 行、绵存一张表 73 行
    横跨 4 个物理页）。关掉之后每页各自成表，页归属在源头就对了。

    曾经以为"关掉续页续接逻辑就没意义了"——**反了**：`paddle_vl.build_quote_csv`
    的 `last_header` 复用本来就是为"没有自己表头的续页"写的，每页独立成表正是
    它的用武之地。实测（每份都比过参照清单）：

    | 文档 | merge=true | merge=false |
    |---|---|---|
    | 泰科龙 | 89 行，页 5-7/9-10/12-14（缺 8、11） | 89 行，页 5-14 **无缺口** |
    | 凯硕 | 90 行，页 4/6 | 90 行，页 4-7；名称错 8→7 处 |
    | 绵存 | **87** 行 | **89 行、逐位零差错** |
    | 宏胜 | 132 行，页 3/6/8/9 | **136 行**，页 3-9 |
    | 亨通 | 132 行，页 2/3/8/9/10 | 132 行，页 2-10 |
    | 浦东 | 263 行，缺页 6/7 | 264 行，页 2-15 |
    | 远东 | 无合并，开关空操作 | 同左（±1 行是换行续行折叠的跑次抖动） |

    招标侧（`paddle_tender.py` 同样吃这个默认值）两臂产出完全一致，不受影响。
    **没有一项变差**，所以是改默认值而不是加开关——留一个"哪次用哪个"的疑问
    没有价值。`paddle_vl._merged_page_spans()` / `SourceRef.page_end` 那条如实标注
    页区间的路留着当兜底：万一某份文档 Paddle 仍然合并，宁可显示"第 7-8 页"，
    也不能把用户送到错的一页。

    `recognize_seal=True`：投标文件封面常见红章，识别出来至少不会污染表格内容。
    这一项仍是 try_paddleocr_vl.py 探索期验证过的默认值。

    `page_count`/`progress_cb`（design/27 §6 补）：轮询期间是唯一能报进度的
    地方——百度轮询响应只有一个状态字段，没有逐页细分（§9 已核实，不是
    "还没接线"，是这条 API 本来就没有更细的信号）。`progress_cb(elapsed_s,
    expected_s)` 每次轮询后调用一次，`expected_s` 按 `page_count` 线性估算
    （`domain_config.PADDLE_EXPECTED_SECONDS_PER_PAGE`）；不传 `page_count`
    时 `expected_s=None`，调用方按"只有已耗时、没有预计耗时"降级展示。本函数
    本身不管进度条怎么显示、封顶多少——那是调用方（`paddle_vl.py`/
    `paddle_tender.py`）的职责，这里只负责把"轮询了多久"如实报出去。
    """
    s = get_settings()
    api_key = s.BAIDU_UNLIMITED_OCR_API_KEY
    secret_key = s.BAIDU_UNLIMITED_OCR_SECRET_KEY
    if not api_key or not secret_key:
        raise ProviderError("BAIDU_UNLIMITED_OCR_API_KEY / _SECRET_KEY 未配置")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    token = _access_token(api_key, secret_key)
    submitted = _post_json(f"{_TASK_URL}?access_token={token}", {
        "file_data": base64.b64encode(file_bytes).decode("ascii"),
        "file_name": file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
        "merge_tables": "true" if merge_tables else "false",
        "recognize_seal": "true" if recognize_seal else "false",
    }, timeout=180, op="提交任务")
    task_id = str((submitted.get("result") or {}).get("task_id") or "")
    if not task_id:
        raise ProviderError(f"Paddle 提交响应没有 task_id：{submitted}")

    from apps.api.core.domain_config import PADDLE_EXPECTED_SECONDS_PER_PAGE
    expected_s = (PADDLE_EXPECTED_SECONDS_PER_PAGE * page_count) if page_count else None
    poll_start = time.monotonic()

    deadline = poll_start + _POLL_TIMEOUT_S
    result: dict = {}
    while time.monotonic() < deadline:
        queried = _post_json(f"{_QUERY_URL}?access_token={token}", {"task_id": task_id},
                             op="查询任务")
        result = queried.get("result") or {}
        status = result.get("status")
        if progress_cb:
            progress_cb(time.monotonic() - poll_start, expected_s)
        if status == "success":
            break
        if status == "failed":
            raise ProviderError(f"Paddle 任务失败（task_id={task_id}）：{result.get('task_error')}")
        time.sleep(_POLL_INTERVAL_S)
    else:
        raise ProviderError(f"Paddle 任务轮询超时（{_POLL_TIMEOUT_S}s），task_id={task_id}")

    parse_result_url = str(result.get("parse_result_url") or "")
    if not parse_result_url:
        raise ProviderError(f"Paddle 任务成功但结果缺 parse_result_url：{result}")
    return _download_json(parse_result_url)
