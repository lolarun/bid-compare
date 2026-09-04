"""Centralised DashScope / OpenAI-compat client factory (§11.5).

All routes and services must obtain their LLM client from here instead of
instantiating OpenAI() inline.  This ensures a single place to swap the
provider, inject auth, and validate configuration.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from apps.api.core.config import get_settings

if TYPE_CHECKING:
    from openai import OpenAI


def get_dashscope_client() -> OpenAI | None:
    """Return a configured DashScope OpenAI-compat client.

    Returns None when no API key is configured, so callers can return a
    graceful error without raising.  Handles the DASHSCOPE_API_KEYS
    comma-list fallback for key rotation.
    """
    from openai import OpenAI

    s = get_settings()
    key: str = s.DASHSCOPE_API_KEY or ""
    if not key:
        multi = getattr(s, "DASHSCOPE_API_KEYS", "") or ""
        if multi:
            key = multi.split(",")[0].strip()
    if not key:
        return None
    from apps.api.core.domain_config import LLM_MAX_RETRIES, LLM_TIMEOUT_S

    return OpenAI(api_key=key, base_url=s.DASHSCOPE_BASE_URL,
                  max_retries=LLM_MAX_RETRIES, timeout=LLM_TIMEOUT_S)


def get_text_client() -> tuple[OpenAI, str] | None:
    """文本类 LLM 调用的**唯一入口**：`(client, model)`，没配 key 时 None。

    本模块的文档一开始就写着"a single place to swap the provider"——这个函数
    就是那句话的落地。此前各调用方各自 `get_dashscope_client()` 再自己挑模型
    （`bid_insight` 写死 `"qwen-plus"`、`enhance` 用 `DASHSCOPE_LLM_MODEL`、
    `block_alignment` 用默认参数 `model="qwen-plus"`），换供应商要改四处，
    而且很容易漏掉一处、造成"大部分切了、有一处还在老供应商上"的分裂状态。

    切换靠 `domain_config.TEXT_CLIENT_VENDOR`（`'dashscope'` | `'mimo'`），
    **2026-08-27 起默认 `mimo`**（此前是 `dashscope`；这段注释一度没跟着改，
    2026-08-28 订正）。配 `mimo` 但没有 `MIMO_API_KEY` 时**明确回落并记日志**，
    不静默降级（`.claude/rules/recognition.md`）——回落是安全网，不是"配不配都
    一样"：不配 key 就等于整个部署还跑在旧厂商上，只有日志能看出来，所以
    `apps/api/.env.example` 把它列成必填项。

    **嵌入（embedding）不走这里**：mimo 没有 embedding 接口，对齐兜底
    （`anchor_match._embed`）必须继续用 dashscope，那是硬约束不是遗漏。
    """
    import logging
    import os

    from apps.api.core.domain_config import TEXT_CLIENT_VENDOR

    log = logging.getLogger(__name__)

    if TEXT_CLIENT_VENDOR == "mimo":
        key = (os.environ.get("MIMO_API_KEY") or "").strip()
        if key:
            from openai import OpenAI

            from apps.api.core.domain_config import (
                LLM_MAX_RETRIES,
                LLM_TIMEOUT_S,
                PAGE_FILTER_BASE_URL,
                PAGE_FILTER_MODEL,
            )
            return OpenAI(api_key=key, base_url=PAGE_FILTER_BASE_URL,
                          max_retries=LLM_MAX_RETRIES,
                          timeout=LLM_TIMEOUT_S), PAGE_FILTER_MODEL
        log.warning("TEXT_CLIENT_VENDOR=mimo 但没有 MIMO_API_KEY，回落 dashscope")

    client = get_dashscope_client()
    if client is None:
        return None
    return client, get_settings().DASHSCOPE_LLM_MODEL
