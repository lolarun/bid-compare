"""test_ocr_provider_concurrency.py — 每个 api_key 的 OCR 并发闸门必须是
**进程级**的，不是每个 provider 实例一份。

背景：provider 有两类构造点——main.py 启动时建一份长驻的（识别任务在
ThreadPoolExecutor 里共用它），而 `scanned_pdf_classify.get_scanned_classify_call()`
是每个 /api/intake/classify-tier0 请求现建一个。信号量若挂在实例上，这两类
调用各自持有一份满配额、互不排队，`OCR_PER_KEY_CONCURRENCY` 就不再是同一个
key 的真实并发上限。这里测的是"分别构造的两个 provider 共用同一个闸门"这条
契约本身（本地契约单测，不发真实请求）。
"""
from __future__ import annotations

import pytest

from apps.api.intelligence.providers import dashscope_ocr as m


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """DASHSCOPE_API_KEYS 优先级高于 api_key 入参，跑测试的机器上可能配了，
    清掉才能确保用的是本测试自己的假 key。"""
    monkeypatch.delenv("DASHSCOPE_API_KEYS", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


def test_two_providers_with_same_key_share_one_semaphore():
    key = "test-shared-key-A"
    a = m.DashScopeOCRProvider(api_key=key)
    b = m.DashScopeOCRProvider(api_key=key)
    assert a._per_key_sem[key] is b._per_key_sem[key]


def test_limit_is_enforced_across_separately_constructed_providers():
    """不只是同一个对象，配额确实是共享的：A 把额度占满后 B 拿不到。"""
    key = "test-shared-key-B"
    a = m.DashScopeOCRProvider(api_key=key)
    b = m.DashScopeOCRProvider(api_key=key)

    taken = 0
    try:
        for _ in range(m._PER_KEY_CONCURRENCY):
            assert a._per_key_sem[key].acquire(blocking=False)
            taken += 1
        # 额度已被 A 占满 —— B 是另一个实例，但不该凭空多出一份配额。
        assert not b._per_key_sem[key].acquire(blocking=False)
    finally:
        for _ in range(taken):
            a._per_key_sem[key].release()


def test_distinct_keys_keep_independent_quotas_and_rotation(monkeypatch):
    """共享不能共享过头：不同 key 各有各的闸门，key 轮转照常工作。"""
    monkeypatch.setenv("DASHSCOPE_API_KEYS", "test-key-C1,test-key-C2")
    p = m.DashScopeOCRProvider()

    assert p._per_key_sem["test-key-C1"] is not p._per_key_sem["test-key-C2"]
    assert [p._next_key() for _ in range(4)] == [
        "test-key-C1", "test-key-C2", "test-key-C1", "test-key-C2",
    ]

    acquired = p._per_key_sem["test-key-C1"].acquire(blocking=False)
    try:
        assert acquired
        # C1 被占用不影响 C2 的配额。
        assert p._per_key_sem["test-key-C2"].acquire(blocking=False)
        p._per_key_sem["test-key-C2"].release()
    finally:
        if acquired:
            p._per_key_sem["test-key-C1"].release()
