# syntax=docker/dockerfile:1.7
# vision-pipeline — Camera Health (Фаза 1), hot path (Фаза 2), SPEC.md §5.
# Отдельное окружение сервиса: ставится только пакет qoima-vision-pipeline и его зависимости (SPEC.md §11).
# База — media-base (FFmpeg под LGPL). PyAV собирается из исходников против этой сборки:
# колёса PyAV с PyPI содержат GPL-FFmpeg (libx264/libx265), это запрещено SPEC.md §2.7.
# Собирается из корня репозитория после media-base: qoima up --build (или install.sh).

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3
ARG MEDIA_BASE=qoima/media-base:dev

FROM ${UV_IMAGE} AS uv

FROM ${MEDIA_BASE} AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package qoima-vision-pipeline --no-binary-package av
# Проверка: PyAV связан с FFmpeg из media-base (qoima-lgpl), а не со своей копией. Строке
# license() верить нельзя: колесо PyPI заявляет LGPL, но содержит libx264/libx265 (GPL).
RUN /app/.venv/bin/python -c "import av._core as c, sys; conf = c.library_meta['libavcodec']['configuration']; bad = [x for x in ('--enable-gpl', 'libx264', 'libx265', '--enable-nonfree') if x in conf]; sys.exit(f'PyAV собран не с LGPL-FFmpeg media-base: {bad or conf}') if bad or 'qoima-lgpl' not in conf else print('PyAV -> FFmpeg qoima-lgpl')"

FROM ${MEDIA_BASE}
RUN groupadd --system --gid 10001 qoima \
 && useradd --system --uid 10001 --gid qoima --home-dir /app --no-create-home qoima
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    QOIMA_SERVICE_PORT=8080
USER qoima
WORKDIR /app
EXPOSE 8080
CMD ["python", "-m", "vision_pipeline"]
