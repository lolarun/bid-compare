# MEMPAS backend — production image
# Multi-stage build: 1) install deps with uv into a virtualenv,
#                    2) copy minimal runtime + venv into a slim final image.

# ─── Stage 1: dependency installer ────────────────────────────────────────
FROM python:3.11-slim AS deps

# uv: fastest pip-compatible installer, single static binary
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Copy only manifest + lockfile first to leverage Docker layer cache.
# Code changes won't bust the dependency layer.
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev


# ─── Stage 2: runtime image ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        # pypdfium2 needs no system deps (statically linked), but tini
        # gives us proper PID-1 signal handling for graceful shutdowns.
        tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user so uploads and database files aren't owned by root
RUN useradd --create-home --uid 1000 mempas

# Copy the prebuilt virtualenv from the deps stage
COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXTRACTION_MODE=thread \
    EXTRACTION_THREAD_POOL_SIZE=8

WORKDIR /app

# Application code (whole apps/api package + its parent __init__.py)
COPY --chown=mempas:mempas apps/__init__.py apps/__init__.py
COPY --chown=mempas:mempas apps/api/ apps/api/

# Data + upload dirs (mounted as volumes in production)
RUN mkdir -p /app/data/uploads && chown -R mempas:mempas /app/data

USER mempas

EXPOSE 8000

# Healthcheck — the orchestrator decides when to restart
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health').read()" || exit 1

# tini → uvicorn → app
# - --workers 4: process-level parallelism for HTTP. Each worker has its
#   own ThreadPoolExecutor (EXTRACTION_THREAD_POOL_SIZE threads) → up to
#   4×8 = 32 concurrent extractions.
# - --proxy-headers: trust X-Forwarded-* from the nginx reverse proxy.
# - --forwarded-allow-ips='*': accept the headers from any source (nginx
#   runs in the adjacent container in our compose, no internet exposure).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "apps.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--access-log", \
     "--log-level", "info"]
