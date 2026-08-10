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

    def spy(content, model, temperature=0.0, stream=False):
        seen["stream"] = stream
        return "row_type,name\ndetail,A"

    provider._mm_call = spy
    provider.vl_extract_csv([b"\x89PNG"], "prompt", model="mdl")
    assert seen["stream"] is True


def test_streamed_response_survives_mm_text(provider):
    """_mm_text 对真实响应取 .output；拼好的流式结果必须走另一条分支而不是崩。"""
    r = m._StreamedResponse("abc")
    assert m.DashScopeOCRProvider._mm_text(r) == "abc"
