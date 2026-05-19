"""Runtime singletons accessible from outside the FastAPI request context.

FastAPI's `app.state` is great for DI within request handlers, but background
tasks run as plain functions where there's no Request available. This module
provides a tiny indirection: the lifespan handler calls `set_runtime_pipeline`
once at startup; background tasks call `get_runtime_pipeline` to read it.

Keep this module side-effect-free. Don't import models or services here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid runtime import cycle
    from apps.api.intelligence.pipeline import ExtractionPipeline

_pipeline: "Optional[ExtractionPipeline]" = None


def set_runtime_pipeline(pipeline: "ExtractionPipeline | None") -> None:
    global _pipeline
    _pipeline = pipeline


def get_runtime_pipeline() -> "Optional[ExtractionPipeline]":
    return _pipeline
