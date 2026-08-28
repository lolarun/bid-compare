"""分类筛页（docs/design/41）—— 三道防线逐条锁住。

**全程离线**：分类器一律用桩函数，桩返回什么由测试指定，因此"模型漏答""窗口
失败""判定波动"这些危险情况能被**正面构造**出来，而不是碰运气碰不到。

这个模块唯一的真实风险是**静默丢页**：判错时那一页从头到尾没被 Paddle 看过，
报价行不是"空格子"（design/33 那种有 `AMOUNT_EMPTY` 信号），而是压根不存在的
行、没有任何下游信号。所以下面的测试重点全部压在"什么情况下**不能**跳过"，
而不是"命中率有多高"。
"""
from __future__ import annotations

import io

import pytest

from apps.api.intelligence import page_filter as pf


def _png(color=(255, 255, 255)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, format="PNG")
    return buf.getvalue()


def _imgs(n: int) -> dict[int, bytes]:
    return {p: _png() for p in range(1, n + 1)}


def _answers(verdicts: dict[int, bool]):
    """造一个按页给定判定的分类器桩。"""
    def _call(images, prompt, *, labels=None):
        pages = [int(l.split("_")[1]) for l in (labels or [])]
        return "\n".join(f"{p},{str(verdicts.get(p, False)).lower()},测试" for p in pages)
    return _call


# ── 默认关闭 ────────────────────────────────────────────────────────────────

def test_no_classifier_sends_everything():
    """没配 key 时本模块等于不存在——行为与接入前逐字节一致。"""
    pages, ledger = pf.select_pages(_imgs(5), classifier=None)
    assert pages == [1, 2, 3, 4, 5]
    assert ledger.skipped == [] and ledger.enabled is False


def test_enabled_flag_is_independent_of_the_mimo_credential(monkeypatch):
    """**开关和凭据是两件事**（2026-08-28）。

    此前"默认关闭"就是"没配 `MIMO_API_KEY`"。但 2026-08-27 起 TEXT/VISION
    厂商默认也是 mimo、读同一个变量，于是"配 key 让厂商默认生效"会顺带打开
    筛页——而筛页的取舍（省 79% Paddle 费、端到端慢 33%）在
    docs/spec/TECHNICAL.md §8 是尚未做出的产品决策。把它钉住：光有 key 不够。
    """
    import apps.api.core.domain_config as dc

    monkeypatch.setenv("MIMO_API_KEY", "sk-present-but-irrelevant")
    monkeypatch.setattr(dc, "PAGE_FILTER_ENABLED", False)
    assert pf.get_production_classifier() is None


def test_credential_is_still_required_when_enabled(monkeypatch):
    """反向也钉住：开了开关但没凭据，仍然返回 None（整份送），
    **不做能力探测后的静默降级**。"""
    import apps.api.core.domain_config as dc

    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(dc, "PAGE_FILTER_ENABLED", True)
    assert pf.get_production_classifier() is None


# ── 防线一：多轮取并集 ──────────────────────────────────────────────────────

def test_union_across_rounds_rescues_a_flaky_miss():
    """**这条是整个设计的支点。**

    实测同一份文档跑两轮，第一轮漏了第 11/12/13 页、第二轮零漏——模型有真实的
    run-to-run 波动，单轮结果不可信。取并集之后只有"每一轮都判否"的页才跳过，
    波动从风险变成了保护。这里把那个实测形态构造出来：第 1 轮漏第 3 页，第 2 轮
    判对，并集必须留住它。
    """
    calls = {"n": 0}

    def _flaky(images, prompt, *, labels=None):
        calls["n"] += 1
        pages = [int(l.split("_")[1]) for l in (labels or [])]
        first_round = calls["n"] <= 1
        out = []
        for p in pages:
            # 第 3 页真值为 true；第一轮故意判否（模拟波动）
            truth = p == 3
            said = False if (first_round and p == 3) else truth
            out.append(f"{p},{str(said).lower()},测试")
        return "\n".join(out)

    pages, ledger = pf.select_pages(_imgs(4), classifier=_flaky, rounds=2)
    assert 3 in pages, "两轮并集没能救回第一轮漏掉的页——防线一失效"
    assert 3 not in ledger.skipped


def test_a_page_all_rounds_call_false_is_skipped():
    """反面：每一轮都明确判否，才允许跳过——否则这个功能就没有省钱效果了。"""
    pages, ledger = pf.select_pages(
        _imgs(4), classifier=_answers({1: True, 2: True, 3: False, 4: False}), rounds=2)
    assert pages == [1, 2]
    assert ledger.skipped == [3, 4]
    assert ledger.skip_reasons[3]


# ── 防线二：一切存疑都送 ────────────────────────────────────────────────────

def test_page_the_model_never_answered_is_sent_not_skipped():
    """模型没提到这一页 → **不能**当成 false。

    静默补 false 就等于把防线拆掉：模型少答一行，一页报价就没了，而且没有任何
    信号。这里桩故意只回前两页的判定，第 3/4 页必须照样送检。
    """
    def _partial(images, prompt, *, labels=None):
        return "1,false,测试\n2,false,测试"      # 只答两页

    pages, ledger = pf.select_pages(_imgs(4), classifier=_partial, rounds=1)
    assert 3 in pages and 4 in pages, "模型漏答的页被静默跳过了"
    assert ledger.skipped == [1, 2]


def test_window_call_failure_sends_those_pages():
    """调用抛异常 → 这一窗一个判定都没有 → 整窗送检，并在台账留错误记录。"""
    def _boom(images, prompt, *, labels=None):
        raise RuntimeError("网关 503")

    pages, ledger = pf.select_pages(_imgs(6), classifier=_boom, rounds=1)
    assert pages == [1, 2, 3, 4, 5, 6]
    assert ledger.skipped == []
    assert ledger.errors and "503" in ledger.errors[0]


def test_unparseable_reply_sends_everything():
    """模型回了一堆没法解析的文本（拒答话术之类）→ 全送，不猜。"""
    def _garbage(images, prompt, *, labels=None):
        return "作为一个人工智能语言模型，我还没学习如何回答这个问题。"

    pages, ledger = pf.select_pages(_imgs(3), classifier=_garbage, rounds=1)
    assert pages == [1, 2, 3] and ledger.skipped == []


# ── 防线三：台账必须闭合 ────────────────────────────────────────────────────

def test_ledger_balances_and_records_reasons():
    """`total == sent + skipped` 恒成立，且每一个被跳过的页都有理由可查。

    这不是装饰性统计——它是「禁止静默截断」在本模块的落地形式：跳过一页这件事
    必须有人能事后查到、且查得到为什么。
    """
    pages, ledger = pf.select_pages(
        _imgs(5), classifier=_answers({1: True, 2: True, 3: True}), rounds=1)
    assert ledger.balanced()
    assert ledger.total_pages == 5
    assert len(ledger.sent) + len(ledger.skipped) == 5
    for p in ledger.skipped:
        assert ledger.skip_reasons.get(p), f"第 {p} 页被跳过却没有理由"
    d = ledger.to_dict()
    assert d["balanced"] is True and d["enabled"] is True


def test_ledger_dict_is_json_serialisable():
    """台账要进 `draft.meta` → 最终落 `job.result`，必须能 JSON 序列化。"""
    import json
    _, ledger = pf.select_pages(_imgs(3), classifier=_answers({1: True}), rounds=1)
    json.dumps(ledger.to_dict(), ensure_ascii=False)


# ── 解析器本身 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expect", [
    ("3,true,表头含数量单价合价", {3: (True, "表头含数量单价合价")}),
    ("PAGE_3,true,带前缀也认", {3: (True, "带前缀也认")}),
    ("3, TRUE , 大小写和空格", {3: (True, "大小写和空格")}),
    ("99,true,不在本窗内的页码丢弃", {}),
    ("这不是一行判定", {}),
])
def test_parse_handles_real_reply_shapes(line, expect):
    """提示词要求"只写数字"，但模型照样可能带 `PAGE_` 前缀——实测遇到过，
    所以解析器两种都认。窗口外的页码一律丢弃，避免串页。"""
    assert pf._parse(line, [1, 2, 3]) == expect


def test_downscale_shrinks_large_images_only():
    from PIL import Image
    big = io.BytesIO()
    Image.new("RGB", (2000, 1400), "white").save(big, format="PNG")
    out = pf._downscale(big.getvalue(), long_edge=900)
    with Image.open(io.BytesIO(out)) as img:
        assert max(img.size) == 900
    small = _png()
    assert pf._downscale(small, long_edge=900) is small   # 本来就小，不动
