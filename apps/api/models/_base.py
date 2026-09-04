"""Shared helpers for models."""

from datetime import UTC, datetime


def _now():
    return datetime.now(UTC)
