"""文本类调用的供应商开关（docs/design/41）。

qwen 在生产里担着四项职责，**失败后果各不相同**，所以是逐项切、不是一次性硬换：

| 职责 | 入口 | 失败后果 |
|---|---|---|
| 封面标量（supplier_name/bid_total） | `pipeline.py` → `extract_quote_meta_from_text` | **最重**：`bid_total` 喂声明总价核对门，读错直接影响入库判定 |
| 招标要求抽取 | `pipeline.py` | 中：影响品牌/技术要求展示 |
| 卡片概述 | `routes/intake.py::compose_summary` | 轻：文案难看，不影响数据 |
| 扫描件招/投标分类 | `routes/intake.py` | 中：路由错文档类型（**视觉调用，不走这个开关**） |

这份测试锁的是**开关本身的行为**，不是"mimo 答得准不准"——后者要真实调用，
属于 fresh E2E，不在离线单测的范围（`.claude/rules/tests.md`：三类证据不得
互相冒充）。
"""
from __future__ import annotations

import pytest

import apps.api.core.domain_config as dc
from apps.api.intelligence import paddle_doc_meta as pdm


def test_default_is_mimo():
    """**2026-08-27 起默认 mimo**——用户明确要求"全部切换为 mimo"（design/41
    的 9 处依赖里 8 处已实测验证），此前默认停在 dashscope 是漏了一步，不是
    用户认可过的决定。没有 MIMO_API_KEY 时仍然回落 dashscope 并记日志，见
    test_mimo_without_key_falls_back_loudly——"默认是 mimo"不等于"没有兜底"。"""
    assert dc.TEXT_CLIENT_VENDOR == "mimo"


def test_mimo_selected_when_configured(monkeypatch):
    monkeypatch.setattr(dc, "TEXT_CLIENT_VENDOR", "mimo")
    monkeypatch.setenv("MIMO_API_KEY", "tp-fake-for-test")
    # 不真的发请求——只验证选到了 mimo 那条分支（客户端 base_url 指向小米）
    captured = {}

    class _FakeOpenAI:
        def __init__(self, api_key, base_url, **kw):
            captured["base_url"] = base_url
            captured["key"] = api_key
            captured["kw"] = kw
        chat = None

    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    call = pdm.get_text_client_call()
    assert call is not None
    assert "xiaomimimo" in captured["base_url"], captured
    assert captured["key"] == "tp-fake-for-test"
    # 重试次数必须是本项目显式声明的值，不是 openai SDK 的默认（2）。断言的是
    # 「有没有被说出来」，不是具体数字——数字改了这里跟着 domain_config 走。
    # 超时在这条路径上是**逐次调用**传的（`paddle_doc_meta._mimo_call`），
    # 不在构造参数里，所以这里只断言重试。
    assert captured["kw"]["max_retries"] == dc.LLM_MAX_RETRIES, captured


def test_mimo_without_key_falls_back_loudly(monkeypatch, caplog):
    """配了 `mimo` 但没有 key → **回落 dashscope 并记日志**。

    静默失败是这个仓库明令禁止的（`.claude/rules/recognition.md`：不做能力探测
    后的静默降级）。回落本身没问题，"回落了但没人知道"才是问题。
    """
    monkeypatch.setattr(dc, "TEXT_CLIENT_VENDOR", "mimo")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)

    called = {}

    def _fake_dashscope():
        called["yes"] = True
        return None            # 没配 dashscope key 时的真实返回

    monkeypatch.setattr("apps.api.services.llm_provider.get_dashscope_client",
                        _fake_dashscope)
    with caplog.at_level("WARNING"):
        pdm.get_text_client_call()
    assert called.get("yes"), "没有回落到 dashscope"
    assert any("MIMO_API_KEY" in r.message for r in caplog.records), \
        "回落了却没有留下任何日志——这正是被禁止的静默降级"


# ── 视觉类调用的开关（design/41）────────────────────────────────────────────
#
# 跟文本开关**分开**是有意的：两类调用的失败后果和验证方式都不同，捆在一起
# 切换等于逼人一次性接受两种风险。

def test_vision_default_is_mimo():
    """2026-08-27 起默认 mimo，跟文本开关同一次改动——理由见
    test_default_is_mimo。"""
    assert dc.VISION_CLIENT_VENDOR == "mimo"


def test_vision_mimo_selected_when_configured(monkeypatch):
    from apps.api.intelligence import scanned_pdf_classify as spc

    monkeypatch.setattr(dc, "VISION_CLIENT_VENDOR", "mimo")
    monkeypatch.setenv("MIMO_API_KEY", "tp-fake")
    captured = {}

    class _FakeOpenAI:
        def __init__(self, api_key, base_url, **kw):
            captured["base_url"] = base_url
            captured["kw"] = kw

    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    call = spc.get_scanned_classify_call()
    assert call is not None
    assert "xiaomimimo" in captured.get("base_url", ""), captured
    assert captured["kw"]["max_retries"] == dc.LLM_MAX_RETRIES, captured


def test_vision_mimo_without_key_falls_back_loudly(monkeypatch, caplog):
    """配 mimo 却没 key → 回落 dashscope **且留日志**。静默降级是被禁止的。"""
    from apps.api.intelligence import scanned_pdf_classify as spc

    monkeypatch.setattr(dc, "VISION_CLIENT_VENDOR", "mimo")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    with caplog.at_level("WARNING"):
        spc.get_scanned_classify_call()
    assert any("MIMO_API_KEY" in r.message for r in caplog.records), \
        "回落了却没有日志——正是被禁止的静默降级"


@pytest.mark.real_gap_filler
def test_gap_fill_follows_the_vision_switch_not_the_text_one(monkeypatch):
    """补位是**视觉**调用，必须跟视觉开关走。

    这条防的是一类具体的错误：把补位挂到文本开关上，于是"只切文本"的人以为
    自己没动视觉，实际上补位已经换了供应商——而补位直接影响金额。
    """
    from apps.api.intelligence import gap_fill

    monkeypatch.setattr(dc, "VISION_CLIENT_VENDOR", "mimo")
    monkeypatch.setattr(dc, "TEXT_CLIENT_VENDOR", "dashscope")
    monkeypatch.setenv("MIMO_API_KEY", "tp-fake")

    class _FakeOpenAI:
        def __init__(self, api_key, base_url, **kw):
            pass

    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    filler = gap_fill.get_production_filler(provider=None)
    assert filler is not None, "视觉开关切到 mimo 后，补位没有跟着切"


def test_embedding_is_documented_as_not_migratable():
    """**embedding 迁不了**：mimo 没有 embedding 接口，对齐兜底只能留在
    dashscope。这条测试不验证行为，它验证的是"这个限制被写下来了"——
    免得后来者以为是漏切了一处。
    """
    import inspect

    from apps.api.services import llm_provider

    doc = inspect.getdoc(llm_provider.get_text_client) or ""
    assert "embedding" in doc.lower(), "embedding 无法迁移这件事没有写进文档"
