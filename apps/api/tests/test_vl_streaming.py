"""VL 长生成必须走流式。

背景：报价清单抽取是长生成（136 行 CSV ≈ 一两万 token）。非流式调用撞上 SDK 的
300s read timeout——read timeout 计的是"多久没收到字节"，不是"总共花了多久"。
实测远东、上海浦东两份因此失败，且 5 次重试全部撞同一堵墙，每份白烧 25 分钟。

调大超时只是把墙往后挪；流式让超时取决于 token 间隔而非生成总长，与文档大小无关。
"""
from __future__ import annotations

import threading
from unittest import mock

import pytest

from apps.api.intelligence.base import ProviderError
from apps.api.intelligence.providers import dashscope_ocr as m


def _chunk(text: str, code: int = 200):
    """伪造一个 MultiModalConversation 分片响应。"""
    msg = type("M", (), {"content": [{"text": text}]})()
    choice = type("C", (), {"message": msg})()
    return type("R", (), {"status_code": code, "message": "",
                          "output": type("O", (), {"choices": [choice]})()})()


@pytest.fixture
def provider():
    p = m.DashScopeOCRProvider.__new__(m.DashScopeOCRProvider)
    p._next_key = lambda: "k"
    p._per_key_sem = {"k": threading.Semaphore(1)}
    return p


def test_chunks_are_concatenated_in_order(provider):
    parts = ["row_type,name\n", "detail,A\n", "detail,B"]
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([_chunk(t) for t in parts])):
        assert provider._mm_call([], "mdl", stream=True) == "".join(parts)


def test_empty_stream_raises_instead_of_returning_empty(provider):
    """空流返回 "" 会被下游当成"识别出零行"——把一次网络问题变成业务结论。"""
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([])):
        with mock.patch.object(m, "_retry_wait", lambda a: 0):
            with pytest.raises(ProviderError, match="no chunks"):
                provider._mm_call([], "mdl", stream=True)


def test_error_status_mid_stream_is_retried(provider):
    calls = {"n": 0}

    def flaky(**_kw):
        calls["n"] += 1
        return iter([_chunk("", 429)]) if calls["n"] == 1 else iter([_chunk("ok")])

    with mock.patch.object(m.dashscope.MultiModalConversation, "call", side_effect=flaky):
        with mock.patch.object(m, "_retry_wait", lambda a: 0):
            assert provider._mm_call([], "mdl", stream=True) == "ok"
    assert calls["n"] == 2


def test_non_streaming_path_unchanged(provider):
    """短响应（页面角色分类等）保持非流式：不为几十个 token 引入分片重组的失败面。"""
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=_chunk("ok")) as c:
        assert provider._mm_call([], "mdl") == "ok"
    assert c.call_args.kwargs.get("stream") is None


def test_quote_extraction_requests_streaming(provider):
    """vl_extract_csv 是那个长生成的调用方——它必须开流式，否则回到超时老路。"""
    seen = {}

    def spy(content, model, temperature=0.0, stream=False, row_progress_cb=None):
        seen["stream"] = stream
        return "row_type,name\ndetail,A"

    provider._mm_call = spy
    provider.vl_extract_csv([b"\x89PNG"], "prompt", model="mdl")
    assert seen["stream"] is True


def test_streamed_response_survives_mm_text(provider):
    """_mm_text 对真实响应取 .output；拼好的流式结果必须走另一条分支而不是崩。"""
    r = m._StreamedResponse("abc")
    assert m.DashScopeOCRProvider._mm_text(r) == "abc"


# ── design/24 B2：row_progress_cb（已转录行数上报）──────────────────────────

def test_row_progress_cb_reports_increasing_line_count(provider):
    """chunk 里带换行符时，累计行数应该单调递增地报给 row_progress_cb。"""
    # 6 个 chunk，累计换行符数 1,2,3,4,5,6 —— 阈值是"每满 5 行才报一次"，
    # 所以只应该在跨过第 5 行时触发一次。
    parts = [f"row{i}\n" for i in range(6)]
    reported = []
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([_chunk(t) for t in parts])):
        provider._mm_call([], "mdl", stream=True, row_progress_cb=reported.append)
    assert reported == [5]


def test_row_progress_cb_not_called_below_threshold(provider):
    """不到 5 行的整个流：一次都不报——不是"至少报一次进度"的语义。"""
    parts = [f"row{i}\n" for i in range(3)]
    reported = []
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([_chunk(t) for t in parts])):
        provider._mm_call([], "mdl", stream=True, row_progress_cb=reported.append)
    assert reported == []


def test_row_progress_cb_none_is_safe(provider):
    """默认（不传 row_progress_cb）：识别路径完全不受影响，改动前的三个测试
    （test_chunks_are_concatenated_in_order 等）已经覆盖这条路径，这里补一句
    显式断言不会因为新参数存在就出问题。"""
    parts = ["a\n", "b\n"]
    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([_chunk(t) for t in parts])):
        assert provider._mm_call([], "mdl", stream=True) == "a\nb\n"


def test_row_progress_cb_exception_does_not_break_stream(provider):
    """进度回调自己炸了，不能把识别本身也炸了——识别结果必须完整拿到。"""
    parts = [f"row{i}\n" for i in range(6)]

    def bad_cb(n):
        raise RuntimeError("boom")

    with mock.patch.object(m.dashscope.MultiModalConversation, "call",
                           return_value=iter([_chunk(t) for t in parts])):
        result = provider._mm_call([], "mdl", stream=True, row_progress_cb=bad_cb)
    assert result == "".join(parts)


def test_vl_extract_csv_forwards_row_progress_cb(provider):
    """vl_extract_csv 是 pipeline.py 实际调用的入口——确认它真的把回调转发到
    _mm_call，不是接了参数却没接线。"""
    seen = {}

    def spy(content, model, temperature=0.0, stream=False, row_progress_cb=None):
        seen["cb"] = row_progress_cb
        return "row_type,name\ndetail,A"

    provider._mm_call = spy
    marker = object()
    provider.vl_extract_csv([b"\x89PNG"], "prompt", model="mdl", row_progress_cb=marker)
    assert seen["cb"] is marker
