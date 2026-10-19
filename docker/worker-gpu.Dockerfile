# syntax=docker/dockerfile:1.7
# worker-gpu — тяжёлые задачи из очереди (SPEC.md §4).
# Отдельное окружение сервиса: ставится только пакет qoima-workers и его зависимости (SPEC.md §11).
# Собирается из корня репозитория: docker compose build (context: ..).
# Фаза 0: python:slim. GPU-библиотеки (TensorRT, PyAV с NVDEC) и CUDA-базовый образ
# добавляются в Фазах 2 и 7 по результатам бенчмарка (docs/TODO.md). nvidia-smi пробрасывает
# nvidia-container-toolkit, поэтому проверка видимости GPU работает уже сейчас.

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3

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
    uv sync --frozen --no-dev --no-editable --package qoima-workers

FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 qoima \
 && useradd --system --uid 10001 --gid qoima --home-dir /app --no-create-home qoima
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    QOIMA_SERVICE_PORT=8080
USER qoima
WORKDIR /app
EXPOSE 8080
CMD ["python", "-m", "workers"]
