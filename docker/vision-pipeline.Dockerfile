# syntax=docker/dockerfile:1.7
# vision-pipeline — Camera Health (Фаза 1), hot path (Фаза 2), SPEC.md §5.
# Отдельное окружение сервиса: ставится только пакет qoima-vision-pipeline и его зависимости (SPEC.md §11).
# База — media-base (FFmpeg под LGPL). PyAV собирается из исходников против этой сборки:
# колёса PyAV с PyPI содержат GPL-FFmpeg (libx264/libx265), это запрещено SPEC.md §2.7.
# Собирается из корня репозитория после media-base: qoima up --build (или install.sh).

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.3
ARG MEDIA_BASE=qoima/media-base:dev
# cpu — onnxruntime (разработка, узлы без GPU); gpu — onnxruntime-gpu + CUDA 13 + cuDNN 9 +
# TensorRT 10 из колёс NVIDIA (GPU-профили compose). Решение Фазы 3 — docs/ASSUMPTIONS.md.
ARG VARIANT=cpu

FROM ${UV_IMAGE} AS uv

FROM ${MEDIA_BASE} AS builder
ARG VARIANT
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
    uv sync --frozen --no-dev --no-editable --package qoima-vision-pipeline --extra "${VARIANT}" \
        --no-binary-package av
# GPU: библиотеки CUDA/cuDNN/TensorRT лежат в колёсах (site-packages/nvidia, tensorrt_libs) —
# регистрируем их каталоги для загрузчика. Проверки: нужные провайдеры есть, и все зависимости
# их библиотек находятся (кроме libcuda — его приносит драйвер через nvidia-container-toolkit).
# TensorRT обязателен на x86_64 (серверы с GPU NVIDIA); в колесе onnxruntime-gpu для aarch64
# провайдера TensorRT нет — там только CUDA (проверено сборкой, docs/ASSUMPTIONS.md).
RUN if [ "${VARIANT}" = "gpu" ]; then \
      find /app/.venv -path '*site-packages/nvidia*' -name 'lib*.so*' -printf '%h\n' > /tmp/gpu-libs \
      && find /app/.venv -path '*site-packages/tensorrt_libs*' -name 'lib*.so*' -printf '%h\n' >> /tmp/gpu-libs \
      && sort -u /tmp/gpu-libs > /app/qoima-gpu-libs.conf \
      && cp /app/qoima-gpu-libs.conf /etc/ld.so.conf.d/ && ldconfig \
      && need="CUDAExecutionProvider" \
      && if [ "$(uname -m)" = "x86_64" ]; then need="${need} TensorrtExecutionProvider"; fi \
      && /app/.venv/bin/python -c "import onnxruntime as o, sys; p = set(o.get_available_providers()); miss = set(sys.argv[1:]) - p; print(sorted(p)); sys.exit(f'нет провайдеров: {sorted(miss)}' if miss else 0)" ${need} \
      && for lib in /app/.venv/lib/python3*/site-packages/onnxruntime/capi/libonnxruntime_providers_*.so; do \
           missing="$(ldd "${lib}" | grep 'not found' | grep -v libcuda.so || true)"; \
           if [ -n "${missing}" ]; then echo "${lib}: не найдены ${missing}"; exit 1; fi; \
         done; \
    else : > /app/qoima-gpu-libs.conf; fi
# Все модули сервиса импортируются в окружении образа: зависимость, которую тянул только
# workspace разработки (как httpx в Фазе 3), здесь уронит сборку, а не запуск на объекте.
RUN /app/.venv/bin/python -c "import pkgutil, importlib, vision_pipeline; [importlib.import_module(m.name) for m in pkgutil.walk_packages(vision_pipeline.__path__, 'vision_pipeline.') if not m.name.endswith('__main__')]; import vision_pipeline.bench.__main__; print('imports ok')"
# Лицензионный гейт по фактическому окружению образа (в GPU-варианте — с колёсами NVIDIA).
RUN /app/.venv/bin/qoima-license-gate python
# Проверка: PyAV связан с FFmpeg из media-base (qoima-lgpl), а не со своей копией. Строке
# license() верить нельзя: колесо PyPI заявляет LGPL, но содержит libx264/libx265 (GPL).
RUN /app/.venv/bin/python -c "import av._core as c, sys; conf = c.library_meta['libavcodec']['configuration']; bad = [x for x in ('--enable-gpl', 'libx264', 'libx265', '--enable-nonfree') if x in conf]; sys.exit(f'PyAV собран не с LGPL-FFmpeg media-base: {bad or conf}') if bad or 'qoima-lgpl' not in conf else print('PyAV -> FFmpeg qoima-lgpl')"

FROM ${MEDIA_BASE}
RUN groupadd --system --gid 10001 qoima \
 && useradd --system --uid 10001 --gid qoima --home-dir /app --no-create-home qoima
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/qoima-gpu-libs.conf /etc/ld.so.conf.d/qoima-gpu-libs.conf
RUN ldconfig
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    QOIMA_SERVICE_PORT=8080
USER qoima
WORKDIR /app
EXPOSE 8080
CMD ["python", "-m", "vision_pipeline"]
