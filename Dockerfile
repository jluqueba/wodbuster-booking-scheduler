# syntax=docker/dockerfile:1.7

# ---- Stage 1: builder -------------------------------------------------------
# Resolves and installs all runtime dependencies into a dedicated prefix so the
# final image carries only what the worker needs at runtime.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build deps required to compile any wheels that ship sdists only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

# Install the package and its runtime dependencies into /install. The dev
# extra is intentionally omitted from the production image.
RUN pip install --upgrade pip \
    && pip install --prefix=/install .

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages"

# Create a non-root user. The container app mounts /data for the SQLite file
# (ADR-0002); the volume permissions are handled by the Container Apps volume
# mount, not by the image.
RUN groupadd --system --gid 1000 worker \
    && useradd  --system --uid 1000 --gid worker --home-dir /app --shell /usr/sbin/nologin worker \
    && mkdir -p /app /data \
    && chown -R worker:worker /app /data

WORKDIR /app

# Copy the resolved environment from the builder stage.
COPY --from=builder /install /install

# Alembic needs its config plus the migration scripts at runtime. The
# entrypoint runs `alembic upgrade head` before starting uvicorn.
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && chown -R worker:worker /app

USER worker

EXPOSE 8000

# Liveness check uses the same /health endpoint that Container Apps and
# Healthchecks.io probe in production (ADR-0006).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# --proxy-headers + --forwarded-allow-ips='*': trust X-Forwarded-Proto
# from Container Apps' ingress so `request.url_for(...)` returns
# https:// URLs (OAuth redirect_uri would otherwise be http:// and
# providers reject it). Kept in sync with the entrypoint fallback.
CMD ["uvicorn", "wodbuster_worker.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
