"""备注 → 份级口径候选的抽取契约（P1）。

用假 client，不打真模型：这里要钉的是**边界行为**（模型乱说/漏答/半截值时系统
怎么办），不是模型准不准。准不准要 fresh E2E 才算数，两者不得互相冒充
（.claude/rules/tests.md）。

原文用的是 docs/test2 真实材料里的句式。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from apps.api.intelligence.basis_extract import (
    SCOPE_VOCAB,
    extract_basis_from_text,
)
from apps.api.models.submission_basis import (
    DIM_COMMODITY_BENCHMARK,
    DIM_DELIVERY_SCOPE,
    DIM_PAYMENT_TERMS,
    STATUS_EXTRACTED,
    STATUS_EXTRACTION_FAILED,
    STATUS_NOT_PRESENT,
)


class _FakeClient:
    """最小 OpenAI 兼容假客户端。`payload` 是模型将要返回的 JSON（或异常）。"""

    def __init__(self, payload):
        self._payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.calls: list[str] = []

    def _create(self, *, model, messages, response_format, timeout):  # noqa: ARG002
        self.calls.append(messages[0]["content"])
        if isinstance(self._payload, Exception):
            raise self._payload
        content = (
            self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=10),
        )


def _by_dim(cands):
    return {c.dim: c for c in cands}


def test_extracts_three_dimensions_from_real_style_text():
    client = _FakeClient({
        "delivery_scope": {"present": True, "quote": "报价不含安装", "said": "不含安装"},
        "commodity_benchmark": {
            "present": True, "quote": "铜价基准：73410元/吨",
            "material": "铜", "price": 73410, "unit": "元/吨",
        },
        "payment_terms": {
            "present": True,
            "quote": "货到现场二个月后支付至送货金额的70%；竣工验收合格后再支付15%",
        },
    })
    out = _by_dim(extract_basis_from_text("（原文）", client=client, model="m"))

    assert out[DIM_DELIVERY_SCOPE].status == STATUS_EXTRACTED
    assert out[DIM_DELIVERY_SCOPE].value["scope"] == "excl_installation"
    assert out[DIM_DELIVERY_SCOPE].value["vocab_hit"] is True
    assert out[DIM_COMMODITY_BENCHMARK].value == {
        "material": "铜", "price": 73410.0, "unit": "元/吨",
    }
    # 原文逐条保留——界面上不许只显示归一值
    assert "70%" in out[DIM_PAYMENT_TERMS].raw_text


def test_candidates_are_never_confirmed():
    """抽取只产候选。门禁只吃 confirmed，所以这里绝不能直接产出 confirmed。"""
    client = _FakeClient({
        d: {"present": True, "quote": "x", "said": "含安装", "material": "铜",
            "price": 1, "unit": "元/吨"}
        for d in (DIM_DELIVERY_SCOPE, DIM_COMMODITY_BENCHMARK, DIM_PAYMENT_TERMS)
    })
    for c in extract_basis_from_text("t", client=client, model="m"):
        assert c.status != "confirmed"


def test_present_false_is_not_present_not_failure():
    """投标方确实没声明 → not_present（业务事实），不是抽取失败。"""
    client = _FakeClient({
        d: {"present": False, "quote": ""}
        for d in (DIM_DELIVERY_SCOPE, DIM_COMMODITY_BENCHMARK, DIM_PAYMENT_TERMS)
    })
    for c in extract_basis_from_text("t", client=client, model="m"):
        assert c.status == STATUS_NOT_PRESENT


def test_missing_key_is_failure_not_not_present():
    """模型漏答某个维度 → extraction_failed。

    这是决策 2 附加约束的核心：漏答和"投标方没写"必须分得开，否则永远查不出
    模型漏抽了多少。
    """
    client = _FakeClient({
        "delivery_scope": {"present": True, "quote": "含安装", "said": "含安装"},
        # 另外两个维度整个没出现
    })
    out = _by_dim(extract_basis_from_text("t", client=client, model="m"))
    assert out[DIM_COMMODITY_BENCHMARK].status == STATUS_EXTRACTION_FAILED
    assert out[DIM_PAYMENT_TERMS].status == STATUS_EXTRACTION_FAILED


def test_malformed_node_is_failure():
    """形状不对（不是 dict / 没有 present）→ 失败，不猜。"""
    client = _FakeClient({"delivery_scope": "含安装"})
    out = _by_dim(extract_basis_from_text("t", client=client, model="m"))
    assert out[DIM_DELIVERY_SCOPE].status == STATUS_EXTRACTION_FAILED


def test_model_error_becomes_failed_not_silent_pass():
    """模型抛错 → 三个维度都 failed，且不向上抛。

    一份文件抽不出来不该让整轮入库失败；但也绝不能静悄悄当成"没有声明"——
    那会让不可比的一轮看起来可比。
    """
    client = _FakeClient(RuntimeError("boom"))
    out = extract_basis_from_text("t", client=client, model="m")
    assert len(out) == 3
    assert all(c.status == STATUS_EXTRACTION_FAILED for c in out)


def test_unparseable_json_becomes_failed():
    client = _FakeClient("这不是 JSON")
    out = extract_basis_from_text("t", client=client, model="m")
    assert all(c.status == STATUS_EXTRACTION_FAILED for c in out)


def test_empty_text_is_failure_not_not_present():
    """手上没有原文 ≠ 投标方没声明。要有人去补原文，不能被当成"没有"放过。"""
    out = extract_basis_from_text("   ", client=_FakeClient({}), model="m")
    assert all(c.status == STATUS_EXTRACTION_FAILED for c in out)


def test_unknown_scope_wording_falls_back_to_other_and_keeps_raw():
    """词表没有的说法 → other + 保留原文，**不硬套最近词条**。

    「含运费不含安装」硬套成「不含安装」会把运费口径悄悄丢掉。
    """
    client = _FakeClient({
        "delivery_scope": {"present": True, "quote": "含运费不含安装",
                           "said": "含运费不含安装"},
    })
    out = _by_dim(extract_basis_from_text("t", client=client, model="m"))
    c = out[DIM_DELIVERY_SCOPE]
    assert c.value["scope"] == "other"
    assert c.value["vocab_hit"] is False
    assert c.raw_text == "含运费不含安装"


def test_benchmark_without_price_is_failure_not_half_value():
    """说有基准却给不出料/价 → 失败，不落半截值。"""
    client = _FakeClient({
        "commodity_benchmark": {"present": True, "quote": "按铜价基准计价",
                                "material": "铜"},   # 没有 price
    })
    out = _by_dim(extract_basis_from_text("t", client=client, model="m"))
    assert out[DIM_COMMODITY_BENCHMARK].status == STATUS_EXTRACTION_FAILED


def test_prompt_carries_no_real_supplier_or_project_names():
    """生产提示词只能用虚构示例（CLAUDE.md §4）。"""
    from apps.api.intelligence.basis_extract import PROMPT

    for forbidden in ("临港", "中科院", "都安", "永旗", "塞克西德", "大航", "凯泉", "东方泵业"):
        assert forbidden not in PROMPT


def test_scope_vocab_is_a_table_not_model_freestyle():
    """归一由词表决定，可 diff 可回滚。"""
    assert SCOPE_VOCAB["不含安装"] == "excl_installation"
    assert SCOPE_VOCAB["安装另计"] == "excl_installation"
    assert SCOPE_VOCAB["含安装"] == "incl_installation"
