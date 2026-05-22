# MEMPAS backend — production image
# Multi-stage build: 1) install deps with uv into a virtualenv,
#                    2) copy minimal runtime + venv into a slim final image.
# All package sources use Chinese mirrors (Aliyun) for mainland ECS.

# ─── Stage 1: dependency installer ────────────────────────────────────────
FROM python:3.11-slim AS deps

# Install uv via pip using Aliyun PyPI mirror
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    UV_HTTP_TIMEOUT=300

WORKDIR /app

# Copy only manifest + lockfile first to leverage Docker layer cache.
# Code changes won't bust the dependency layer.
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev


# ─── Stage 2: runtime image ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Use Aliyun Debian mirror for apt
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
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
# - --workers 2: 2 vCPU ECS, one worker per core. Each worker has its
#   own ThreadPoolExecutor (EXTRACTION_THREAD_POOL_SIZE threads) → up to
#   2×8 = 16 concurrent extractions.
# - --proxy-headers: trust X-Forwarded-* from the nginx reverse proxy.
# - --forwarded-allow-ips='*': accept the headers from any source (nginx
#   runs in the adjacent container in our compose, no internet exposure).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "apps.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips=*", \
     "--access-log", \
     "--log-level", "info"]
