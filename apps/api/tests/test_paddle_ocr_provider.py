"""docs/design/26 §5（轨 P1）：PaddleOCR-VL 提交/轮询 provider 测试。

全部 mock `urlopen`/`time.sleep`，不打真实网络——跟 `vl_quote.py` 的 `VLCall`
注入同一个可测试性原则（`.claude/rules/recognition.md`）。覆盖：
- 正常提交→轮询成功→下载结果的整条链路
- 网络层失败重试后成功
- 业务层 error_code!=0 重试后成功
- 两层都耗尽重试后抛 ProviderError（且错误信息里带得出最后一次失败原因）
- 任务失败状态 / 轮询超时 / 响应缺关键字段 / 凭据未配置 分别抛 ProviderError
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apps.api.intelligence.base import ProviderError
from apps.api.intelligence.providers import paddle_ocr


class _FakeResponse:
    """伪装 urlopen() 的上下文管理器返回值。"""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(paddle_ocr.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    monkeypatch.setattr(paddle_ocr, "get_settings", lambda: SimpleNamespace(
        BAIDU_UNLIMITED_OCR_API_KEY="test-key", BAIDU_UNLIMITED_OCR_SECRET_KEY="test-secret",
    ))


def _seq_urlopen(responses: list):
    """按调用顺序依次返回/抛出 responses 里的项——item 是 dict 就包成
    _FakeResponse，是 Exception 就抛出。"""
    it = iter(responses)

    def _mock(req, timeout=None):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)
    return _mock


def _write_tmp_pdf(tmp_path) -> str:
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return str(p)


# ─── §1 正常链路 ─────────────────────────────────────────────────────────────

def test_submit_and_parse_happy_path(tmp_path, monkeypatch):
    parsed = {"pages": [{"page_num": 0, "tables": []}]}
    calls = [
        {"error_code": 0, "access_token": "tok"},                                    # token
        {"error_code": 0, "result": {"task_id": "T1"}},                              # submit
        {"error_code": 0, "result": {"status": "success",
                                     "parse_result_url": "https://x/result.json"}},   # query
        parsed,                                                                       # download
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    result = paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))
    assert result == parsed


def test_submit_and_parse_polls_until_success(tmp_path, monkeypatch):
    parsed = {"pages": []}
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
        {"error_code": 0, "result": {"status": "running"}},   # 第一次查询：还没好
        {"error_code": 0, "result": {"status": "running"}},   # 第二次：还没好
        {"error_code": 0, "result": {"status": "success", "parse_result_url": "https://x/r.json"}},
        parsed,
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    result = paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))
    assert result == parsed


# ─── §2 重试 ─────────────────────────────────────────────────────────────────

def test_post_json_retries_on_network_error_then_succeeds(monkeypatch):
    from urllib.error import URLError
    calls = [URLError("timeout"), URLError("timeout"), {"error_code": 0, "ok": True}]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    result = paddle_ocr._post_json("https://x", {}, op="test")
    assert result == {"error_code": 0, "ok": True}


def test_post_json_retries_on_business_error_then_succeeds(monkeypatch):
    calls = [
        {"error_code": 4, "error_msg": "qps limit"},   # 模拟限流类响应
        {"error_code": 0, "ok": True},
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    result = paddle_ocr._post_json("https://x", {}, op="test")
    assert result == {"error_code": 0, "ok": True}


def test_post_json_raises_after_exhausting_all_retries(monkeypatch):
    from urllib.error import URLError
    calls = [URLError("down")] * paddle_ocr._MAX_RETRIES
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    with pytest.raises(ProviderError, match="重试.*次仍失败"):
        paddle_ocr._post_json("https://x", {}, op="test")


def test_post_json_error_message_carries_last_failure_reason(monkeypatch):
    calls = [{"error_code": 17, "error_msg": "每日调用量超限"}] * paddle_ocr._MAX_RETRIES
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    with pytest.raises(ProviderError, match="每日调用量超限"):
        paddle_ocr._post_json("https://x", {}, op="test")


# ─── §3 各类失败路径 ─────────────────────────────────────────────────────────

def test_submit_and_parse_raises_when_task_id_missing(tmp_path, monkeypatch):
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {}},   # 提交成功但没给 task_id
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    with pytest.raises(ProviderError, match="task_id"):
        paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))


def test_submit_and_parse_raises_on_task_failed_status(tmp_path, monkeypatch):
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
        {"error_code": 0, "result": {"status": "failed", "task_error": "文件损坏"}},
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    with pytest.raises(ProviderError, match="文件损坏"):
        paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))


def test_submit_and_parse_raises_on_poll_timeout(tmp_path, monkeypatch):
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))

    # 让 deadline 瞬间过期，不用真的等 900s。
    t = {"n": 0}
    def _fake_monotonic():
        t["n"] += 1
        return 0 if t["n"] == 1 else paddle_ocr._POLL_TIMEOUT_S + 1
    monkeypatch.setattr(paddle_ocr.time, "monotonic", _fake_monotonic)
    with pytest.raises(ProviderError, match="轮询超时"):
        paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))


def test_submit_and_parse_raises_when_result_url_missing(tmp_path, monkeypatch):
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
        {"error_code": 0, "result": {"status": "success"}},   # 成功但没给 URL
    ]
    monkeypatch.setattr(paddle_ocr, "urlopen", _seq_urlopen(calls))
    with pytest.raises(ProviderError, match="parse_result_url"):
        paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))


def test_submit_and_parse_raises_when_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(paddle_ocr, "get_settings", lambda: SimpleNamespace(
        BAIDU_UNLIMITED_OCR_API_KEY="", BAIDU_UNLIMITED_OCR_SECRET_KEY="",
    ))
    with pytest.raises(ProviderError, match="未配置"):
        paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))


# ── 提交参数 ────────────────────────────────────────────────────────────────
# merge_tables 2026-08-22 由 True 改为 False：开着的时候 Paddle 把跨页续表整段行
# 塞进 begin 那一页，续页的行全部继承错误页码（泰科龙 19/89 行、绵存 73 行）。
# 7 份报价件 + 2 份招标件实测，关掉后页归属全对、召回同等或更好（绵存 87→89 行、
# 宏胜 132→136 行）。这个默认值是实测结论，改回去要先拿出新的实测。

def test_submit_sends_merge_tables_false_by_default(tmp_path, monkeypatch):
    from urllib.parse import parse_qs

    seen: list[dict] = []
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
        {"error_code": 0, "result": {"status": "success",
                                     "parse_result_url": "https://x/result.json"}},
        {"pages": []},
    ]
    inner = _seq_urlopen(calls)

    def _spy(req, *a, **kw):
        data = getattr(req, "data", None)
        if data:
            seen.append({k: v[0] for k, v in parse_qs(data.decode("utf-8")).items()})
        return inner(req, *a, **kw)

    monkeypatch.setattr(paddle_ocr, "urlopen", _spy)
    paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path))

    submit = next(f for f in seen if "file_data" in f)
    assert submit["merge_tables"] == "false"
    assert submit["recognize_seal"] == "true"


def test_submit_merge_tables_can_still_be_turned_on(tmp_path, monkeypatch):
    """开关本身还在——改的是默认值，不是把能力删掉（对照实验还要用它）。"""
    from urllib.parse import parse_qs

    seen: list[dict] = []
    calls = [
        {"error_code": 0, "access_token": "tok"},
        {"error_code": 0, "result": {"task_id": "T1"}},
        {"error_code": 0, "result": {"status": "success",
                                     "parse_result_url": "https://x/result.json"}},
        {"pages": []},
    ]
    inner = _seq_urlopen(calls)

    def _spy(req, *a, **kw):
        data = getattr(req, "data", None)
        if data:
            seen.append({k: v[0] for k, v in parse_qs(data.decode("utf-8")).items()})
        return inner(req, *a, **kw)

    monkeypatch.setattr(paddle_ocr, "urlopen", _spy)
    paddle_ocr.submit_and_parse(_write_tmp_pdf(tmp_path), merge_tables=True)

    assert next(f for f in seen if "file_data" in f)["merge_tables"] == "true"
