"""design/24 B4 —— batch-confirm dry-run 结果缓存。

收件箱每次重新打开都会对着同一批未变化的文件重新跑一遍 dry-run；不缓存的话
就是 N 份文件 × 每次进收件箱都全量重验一遍（含 DB 查询）。按 job_id + 请求
相关字段的内容哈希做键——内容变了（用户编辑表格/切换品类/换供应商）键自然
跟着变，等于失效，不需要显式过期动作。

**进程内缓存，非分布式**：本仓库单进程部署（CLAUDE.md §3：固定端口、禁用
--reload），多 worker/多实例场景不适用——届时需要换成外部缓存，这里先不做，
不是这轮的问题。
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

_MAX_ENTRIES = 500
_lock = threading.Lock()
_cache: dict[str, dict] = {}
_order: list[str] = []   # insertion order，简单 FIFO 淘汰（不是严格 LRU，够用）


def _hash(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def cache_key(job_id: str, items: list[dict], *, category: str, supplier_id: int | None,
              checksum_ack: bool) -> str:
    """items 内容 + 影响判定结果的请求字段一起入哈希——只哈希 items 会漏掉
    "同一份文件，用户把 category 从阀门改成给排水后重新预览"这类场景。
    """
    return f"{job_id}:{_hash({'items': items, 'category': category, 'supplier_id': supplier_id, 'checksum_ack': checksum_ack})}"


def get(key: str) -> dict | None:
    with _lock:
        return _cache.get(key)


def put(key: str, result: dict) -> None:
    with _lock:
        if key not in _cache and len(_order) >= _MAX_ENTRIES:
            oldest = _order.pop(0)
            _cache.pop(oldest, None)
        if key not in _cache:
            _order.append(key)
        _cache[key] = result


def invalidate_job(job_id: str) -> None:
    """job 本身发生了新的识别结果（重新上传/重新识别）时的双保险清理——正常
    情况下 items 内容一变哈希键就跟着变，不命中旧缓存；这里按 job_id 前缀扫一遍
    只是防止极端情况下的键复用。"""
    prefix = f"{job_id}:"
    with _lock:
        stale = [k for k in _order if k.startswith(prefix)]
        for k in stale:
            _cache.pop(k, None)
            _order.remove(k)
