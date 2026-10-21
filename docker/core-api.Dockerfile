# syntax=docker/dockerfile:1.7
# core-api — REST/WebSocket API, автопоиск и зондирование камер, раздача UI (SPEC.md §4).
# Отдельное окружение сервиса: ставится только пакет qoima-core-api и его зависимости (SPEC.md §11).
# База — media-base (FFmpeg под LGPL). PyAV собирается из исходников против этой сборки:
# колёса PyAV с PyPI содержат GPL-FFmpeg (libx264/libx265), это запрещено SPEC.md §2.7.
# Собирается из корня репозитория после media-base: qoima up --build (или install.sh).

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3
ARG MEDIA_BASE=qoima/media-base:dev

ARG NODE_IMAGE=node:22.23.2-bookworm-slim

# --- сборка UI (React + Vite + TypeScript)
FROM ${NODE_IMAGE} AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

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
    uv sync --frozen --no-dev --no-editable --package qoima-core-api --no-binary-package av
# Проверка: PyAV связан с FFmpeg из media-base (qoima-lgpl), а не со своей копией. Строке
# license() верить нельзя: колесо PyPI заявляет LGPL, но содержит libx264/libx265 (GPL).
RUN /app/.venv/bin/python -c "import av._core as c, sys; conf = c.library_meta['libavcodec']['configuration']; bad = [x for x in ('--enable-gpl', 'libx264', 'libx265', '--enable-nonfree') if x in conf]; sys.exit(f'PyAV собран не с LGPL-FFmpeg media-base: {bad or conf}') if bad or 'qoima-lgpl' not in conf else print('PyAV -> FFmpeg qoima-lgpl')"

# Все модули сервиса импортируются в окружении образа (см. vision-pipeline.Dockerfile).
RUN /app/.venv/bin/python -c "import pkgutil, importlib, core_api; [importlib.import_module(m.name) for m in pkgutil.walk_packages(core_api.__path__, 'core_api.') if '.migrations.' not in m.name and not m.name.endswith('__main__')]; print('imports ok')"

FROM ${MEDIA_BASE}
# Шрифт с кириллицей и казахскими буквами для PDF-отчёта плана мощности (DejaVu — свободная
# лицензия Bitstream Vera + public domain, docs/DATA_LICENSES.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
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
