# syntax=docker/dockerfile:1
# ---- Builder: produce a wheel from source ----------------------------------------------
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

# ---- Base: slim runtime image (markdownify-only converter) -----------------------------
FROM python:3.12-slim AS base
LABEL org.opencontainers.image.title="quip-vault-exporter" \
      org.opencontainers.image.description="Read-only Quip workspace -> Obsidian-ready Markdown vault exporter" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/GoldTechMx/quip-vault-exporter" \
      org.opencontainers.image.version="0.1.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

WORKDIR /app

# Reproducible installs: pinned deps first (cached layer), then the wheel with --no-deps.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl && rm -f /tmp/*.whl

# Non-root; export output is bind-mounted at /app/exports.
RUN useradd --create-home --uid 10001 exporter \
    && mkdir -p /app/exports \
    && chown -R exporter:exporter /app
USER exporter

ENTRYPOINT ["quip-vault-exporter"]
CMD ["--help"]

# ---- Web: adds the FastAPI/uvicorn UI (run `serve`) ------------------------------------
FROM base AS web
USER root
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29"
USER exporter
EXPOSE 8000
# Bind 0.0.0.0 inside the container; publish the port and front it with your own auth.
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]

# ---- Full: optional Pandoc backend for higher-fidelity Markdown ------------------------
FROM base AS full
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*
USER exporter
