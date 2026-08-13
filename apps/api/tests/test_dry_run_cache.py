"""design/24 B4 —— dry-run 结果缓存单测。"""
from __future__ import annotations

from apps.api.services.submission import dry_run_cache as cache


def _reset():
    cache._cache.clear()
    cache._order.clear()


def test_same_input_same_key():
    _reset()
    items = [{"material": "A", "total_price": 100}]
    k1 = cache.cache_key("job1", items, category="阀门", supplier_id=1, checksum_ack=False)
    k2 = cache.cache_key("job1", items, category="阀门", supplier_id=1, checksum_ack=False)
    assert k1 == k2


def test_edited_items_different_key():
    """用户编辑表格（items 内容变了）→ 键跟着变 → 天然失效，不需要显式过期。"""
    _reset()
    k1 = cache.cache_key("job1", [{"material": "A", "total_price": 100}],
                          category="阀门", supplier_id=1, checksum_ack=False)
    k2 = cache.cache_key("job1", [{"material": "A", "total_price": 200}],
                          category="阀门", supplier_id=1, checksum_ack=False)
    assert k1 != k2


def test_different_category_different_key():
    """同一份 items，只是品类选择不同 → 也是不同键（判定结果会跟着变）。"""
    _reset()
    items = [{"material": "A", "total_price": 100}]
    k1 = cache.cache_key("job1", items, category="阀门", supplier_id=None, checksum_ack=False)
    k2 = cache.cache_key("job1", items, category="给排水", supplier_id=None, checksum_ack=False)
    assert k1 != k2


def test_checksum_ack_different_key():
    _reset()
    items = [{"material": "A", "total_price": 100}]
    k1 = cache.cache_key("job1", items, category="阀门", supplier_id=None, checksum_ack=False)
    k2 = cache.cache_key("job1", items, category="阀门", supplier_id=None, checksum_ack=True)
    assert k1 != k2


def test_get_put_roundtrip():
    _reset()
    key = cache.cache_key("job1", [{"material": "A"}], category="阀门",
                           supplier_id=None, checksum_ack=False)
    assert cache.get(key) is None
    cache.put(key, {"would_succeed": True})
    assert cache.get(key) == {"would_succeed": True}


def test_invalidate_job_clears_only_that_job():
    _reset()
    k1 = cache.cache_key("job1", [{"material": "A"}], category="阀门",
                          supplier_id=None, checksum_ack=False)
    k2 = cache.cache_key("job2", [{"material": "B"}], category="阀门",
                          supplier_id=None, checksum_ack=False)
    cache.put(k1, {"job": 1})
    cache.put(k2, {"job": 2})
    cache.invalidate_job("job1")
    assert cache.get(k1) is None
    assert cache.get(k2) == {"job": 2}


def test_eviction_bounds_cache_size(monkeypatch):
    """超过上限时按插入顺序淘汰最老的一条，不无限增长。"""
    _reset()
    monkeypatch.setattr(cache, "_MAX_ENTRIES", 3)
    keys = [
        cache.cache_key(f"job{i}", [{"material": str(i)}], category="阀门",
                         supplier_id=None, checksum_ack=False)
        for i in range(5)
    ]
    for k in keys:
        cache.put(k, {"k": k})
    assert len(cache._cache) == 3
    # 最早插入的两个应该已经被淘汰
    assert cache.get(keys[0]) is None
    assert cache.get(keys[1]) is None
    assert cache.get(keys[4]) == {"k": keys[4]}
