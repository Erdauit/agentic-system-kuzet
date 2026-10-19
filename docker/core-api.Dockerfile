# syntax=docker/dockerfile:1.7
# core-api — REST/WebSocket API и раздача UI (SPEC.md §4).
# Отдельное окружение сервиса: ставится только пакет qoima-core-api и его зависимости (SPEC.md §11).
# Собирается из корня репозитория: docker compose build (context: ..).

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3

ARG NODE_IMAGE=node:22.23.2-bookworm-slim

# --- сборка UI (React + Vite + TypeScript)
FROM ${NODE_IMAGE} AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package qoima-core-api

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 qoima \
 && useradd --system --uid 10001 --gid qoima --home-dir /app --no-create-home qoima
COPY --from=builder /app/.venv /app/.venv
COPY --from=ui /ui/dist /app/ui
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER qoima
WORKDIR /app
EXPOSE 8443
CMD ["python", "-m", "core_api"]
