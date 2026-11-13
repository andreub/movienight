FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    MOVIENIGHT_DATA_DIR=/data \
    MOVIENIGHT_LETTERBOXD_DIR=/letterboxd

WORKDIR /app

# Layer 1: dependencies only (cached until the lock file changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: the project itself
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN groupadd --system --gid 10001 movienight \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin movienight \
    && mkdir -p /data /letterboxd \
    && chown 10001:10001 /data

USER movienight

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "movienight.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
