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


def get_dashscope_client() -> "OpenAI | None":
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
    return OpenAI(api_key=key, base_url=s.DASHSCOPE_BASE_URL)
