FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY docs ./docs
COPY samples ./samples
COPY agents.md architecture.yml .mcp.json ./
COPY ops/docker/entrypoint.sh /usr/local/bin/synode-entrypoint

RUN pip install --upgrade pip \
    && pip install --editable . \
    && useradd --create-home --uid 1000 --user-group --shell /usr/sbin/nologin synode \
    && chown -R synode:synode /app \
    && chmod 0755 /usr/local/bin/synode-entrypoint

USER synode

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD curl -fsS http://127.0.0.1:8787/health || exit 1

ENTRYPOINT ["synode-entrypoint"]
CMD ["serve"]
