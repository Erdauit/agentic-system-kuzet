# Лицензии зависимостей и данных

Белый список и запреты — SPEC.md §2.7 и `src/shared/licenses/policy.yaml`. Таблицы ниже
сгенерированы лицензионным гейтом (`qoima-license-gate python|npm --format markdown`) на
окружении разработки и должны обновляться при каждом изменении `uv.lock` / `ui/package-lock.json`.
CI проверяет актуальное состояние на каждом коммите — эти таблицы служат реестром для заказчика.

## Пакеты вне белого списка (после технического ревью)

| Пакет | Лицензия | Почему допустимо | Статус |
|---|---|---|---|
| certifi | MPL-2.0 | CA-бандл для httpx, без модификаций; файловый copyleft MPL не распространяется на код Qoima | нужно подтверждение юриста |
| typing-extensions | PSF-2.0 | обязательная зависимость pydantic; пермиссивная лицензия | нужно подтверждение юриста |
| greenlet | MIT AND PSF-2.0 | зависимость SQLAlchemy asyncio; обе лицензии пермиссивные | нужно подтверждение юриста |
| pathspec | MPL-2.0 | только dev-зависимость mypy, в образы не попадает | нужно подтверждение юриста |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | numpy и вендорные фрагменты, все пермиссивные | нужно подтверждение юриста |

## Нативные библиотеки внутри колёс и образов

Гейт видит только метаданные Python/npm-пакетов. Нативные библиотеки, которые приезжают внутри
колёс и образов, учтены вручную (`src/shared/licenses/components.yaml: native`):

| Компонент | Где | Лицензия | Статус |
|---|---|---|---|
| FFmpeg 9.0.2 (своя сборка) | media-base → core-api, vision-pipeline, camera-sim | LGPL-2.1-or-later, без GPL/nonfree (проверяется при сборке) | нужно подтверждение юриста (LGPL, динамическая линковка) |
| libopenh264 | media-base | BSD-2-Clause | кодировщик нужен только симулятору |
| PyAV из PyPI (колесо) | **не используется в образах** | заявлено LGPL, но содержит libx264/libx265 (GPL) | в образах PyAV собирается из исходников |
| onnxruntime 1.31 (колесо, MIT) | vision-pipeline | MIT; внутри статически — protobuf (BSD-3), abseil (Apache-2.0), Eigen (MPL-2.0, заголовочная), flatbuffers (Apache-2.0) и др. по ThirdPartyNotices | нужно подтверждение юриста (MPL-2.0 Eigen, файловый copyleft без модификаций) |
| opencv-python-headless 4.14 | vision-pipeline | Apache-2.0; внутри FFmpeg (LGPL-2.1), OpenSSL 1.1.1, libgfortran (GPL-3.0 WITH GCC-exception-3.1), OpenBLAS (BSD-3), libpng, libvpx/libaom/dav1d (BSD) | нужно подтверждение юриста; OpenSSL 1.1.1 снят с поддержки — заменить при обновлении |



## Docker-образы

Реестр — `src/shared/licenses/components.yaml`, проверяется тестом `tests/unit/test_compose_files.py`.

| Образ | Лицензия | Примечание |
|---|---|---|
| postgres:16.15-bookworm | PostgreSQL | |
| valkey/valkey:8.1.10 | BSD-3-Clause | замена Redis (RSAL/SSPL) |
| qdrant/qdrant:v1.19.1 | Apache-2.0 | телеметрия отключена |
| bluenviron/mediamtx:1.21.2-ffmpeg | MIT | ffmpeg внутри — отдельная программа; нужно подтверждение юриста |
| prom/prometheus:v3.15.0-busybox | Apache-2.0 | |
| prom/node-exporter:v1.12.1-busybox | Apache-2.0 | |
| nvidia/dcgm-exporter:4.6.1-4.8.4 | Apache-2.0 | только GPU-профили |
| nvcr.io/nvidia/tritonserver:26.08-py3 | BSD-3-Clause + NVIDIA DL Container License | только GPU-профили |
| grafana/grafana:12.4.13 | AGPL-3.0 | **только** опциональный профиль `engineering`, не входит в поставку |

## Датасеты и веса моделей

| Модель | Веса | Данные обучения | Где | Статус |
|---|---|---|---|---|
| RF-DETR (детектор людей) | Apache-2.0 (Roboflow) | COCO 2017: аннотации CC BY 4.0, изображения — Flickr, условия Flickr ToU | готовит `tools/models/prepare_rfdetr.py`, в репозитории нет | веса не скачаны: нужен запуск инженером Qoima |
| RTMPose-m body7 (поза) | Apache-2.0 (OpenMMLab) | Body7: COCO, MPII, CrowdPose, AI Challenger, Halpe, PoseTrack18, sub-JHMDB — у части датасетов некоммерческие условия | шаблон `models/registry/rtmpose-body.yaml`, артефакт в репозитории нет | **нужно ревью лицензий датасетов** до поставки; альтернатива — RF-DETR keypoints (выбор по бенчмарку) |
| qoima-smoke-fire | веса Qoima | данные Qoima | нет | весов нет — сценарий «не развёрнут» |

Лицензия весов указывается в манифесте (`ModelManifest.license`) и проверяется политикой при
загрузке модели; sha256 артефакта сверяется с манифестом.

## Python (все пакеты окружения разработки)

| Экосистема | Пакет | Версия | Лицензия | Решение |
|---|---|---|---|---|
| python | alembic | 1.20.0 | MIT | allowed |
| python | annotated-doc | 0.0.5 | MIT | allowed |
| python | annotated-types | 0.8.0 | MIT | allowed |
| python | anyio | 4.15.1 | MIT | allowed |
| python | asyncpg | 0.32.0 | Apache-2.0 | allowed |
| python | av | 19.0.1 | BSD-3-Clause | allowed |
| python | certifi | 2026.10.18 | MPL-2.0 | reviewed |
| python | click | 8.5.0 | BSD-3-Clause | allowed |
| python | fastapi | 0.147.0 | MIT | allowed |
| python | flatbuffers | 25.12.19 | Apache-2.0 | allowed |
| python | greenlet | 3.5.6 | MIT AND PSF-2.0 | reviewed |
| python | h11 | 0.16.0 | MIT | allowed |
| python | httpcore | 1.0.9 | BSD-3-Clause | allowed |
| python | httpx | 0.28.1 | BSD-3-Clause | allowed |
| python | idna | 3.20 | BSD-3-Clause | allowed |
| python | iniconfig | 2.3.1 | MIT | allowed |
| python | librt | 0.16.0 | MIT | allowed |
| python | Mako | 1.4.3 | MIT | allowed |
| python | markdown-it-py | 4.2.0 | MIT | allowed |
| python | MarkupSafe | 3.0.4 | BSD-3-Clause | allowed |
| python | mdurl | 0.1.2 | MIT | allowed |
| python | ml_dtypes | 0.6.0 | Apache-2.0 | allowed |
| python | mypy | 1.20.2 | MIT | allowed |
| python | mypy_extensions | 1.1.0 | MIT | allowed |
| python | numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | reviewed |
| python | onnx | 1.23.2 | Apache-2.0 | allowed |
| python | onnxruntime | 1.31.0 | MIT | allowed |
| python | opencv-python-headless | 4.14.0.94 | Apache-2.0 | allowed |
| python | opentelemetry-api | 1.45.1 | Apache-2.0 | allowed |
| python | packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | allowed |
| python | pathspec | 1.1.1 | MPL-2.0 | reviewed |
| python | pluggy | 1.7.0 | MIT | allowed |
| python | prometheus_client | 0.26.0 | Apache-2.0 AND BSD-2-Clause | allowed |
| python | protobuf | 7.36.2 | BSD-3-Clause | allowed |
| python | pydantic | 2.14.1 | MIT | allowed |
| python | pydantic_core | 2.50.1 | MIT | allowed |
| python | Pygments | 2.21.0 | BSD-2-Clause | allowed |
| python | pymodbus | 3.16.1 | BSD-3-Clause | allowed |
| python | pytest | 9.1.1 | MIT | allowed |
| python | pytest-asyncio | 1.4.0 | Apache-2.0 | allowed |
| python | PyYAML | 6.0.3 | MIT | allowed |
| python | rich | 15.0.0 | MIT | allowed |
| python | ruff | 0.17.1 | MIT | allowed |
| python | shellingham | 1.5.4 | ISC | allowed |
| python | SQLAlchemy | 2.0.55 | MIT | allowed |
| python | starlette | 1.9.0 | BSD-3-Clause | allowed |
| python | structlog | 26.1.0 | MIT OR Apache-2.0 | allowed |
| python | typer | 0.27.3 | MIT | allowed |
| python | types-PyYAML | 6.0.12.20260906 | Apache-2.0 | allowed |
| python | typing-inspection | 0.4.4 | MIT | allowed |
| python | typing_extensions | 4.16.0 | PSF-2.0 | reviewed |
| python | tzdata | 2026.5 | Apache-2.0 | allowed |
| python | uvicorn | 0.54.0 | BSD-3-Clause | allowed |
| python | valkey | 6.2.0 | MIT | allowed |

## npm (ui/package-lock.json)

| Экосистема | Пакет | Версия | Лицензия | Решение |
|---|---|---|---|---|
| npm | @esbuild/aix-ppc64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/android-arm | 0.28.2 | MIT | allowed |
| npm | @esbuild/android-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/android-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/darwin-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/darwin-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/freebsd-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/freebsd-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-arm | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-ia32 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-loong64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-mips64el | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-ppc64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-riscv64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-s390x | 0.28.2 | MIT | allowed |
| npm | @esbuild/linux-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/netbsd-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/netbsd-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/openbsd-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/openbsd-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/openharmony-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/sunos-x64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/win32-arm64 | 0.28.2 | MIT | allowed |
| npm | @esbuild/win32-ia32 | 0.28.2 | MIT | allowed |
| npm | @esbuild/win32-x64 | 0.28.2 | MIT | allowed |
| npm | @napi-rs/lzma-linux-x64-gnu | 1.5.1 | MIT | allowed |
| npm | @rolldown/pluginutils | 1.0.1 | MIT | allowed |
| npm | @rollup/rollup-android-arm-eabi | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-android-arm64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-darwin-arm64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-darwin-x64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-freebsd-arm64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-freebsd-x64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-arm-gnueabihf | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-arm-musleabihf | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-arm64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-arm64-musl | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-loong64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-loong64-musl | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-ppc64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-ppc64-musl | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-riscv64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-riscv64-musl | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-s390x-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-x64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-linux-x64-musl | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-openbsd-x64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-openharmony-arm64 | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-win32-arm64-msvc | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-win32-ia32-msvc | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-win32-x64-gnu | 4.64.10 | MIT | allowed |
| npm | @rollup/rollup-win32-x64-msvc | 4.64.10 | MIT | allowed |
| npm | @swc/core | 1.16.18 | Apache-2.0 | allowed |
| npm | @swc/core-darwin-arm64 | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-darwin-x64 | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-arm-gnueabihf | 1.16.18 | Apache-2.0 | allowed |
| npm | @swc/core-linux-arm64-gnu | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-arm64-musl | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-ppc64-gnu | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-s390x-gnu | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-x64-gnu | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-linux-x64-musl | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-win32-arm64-msvc | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-win32-ia32-msvc | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/core-win32-x64-msvc | 1.16.18 | Apache-2.0 AND MIT | allowed |
| npm | @swc/counter | 0.1.3 | Apache-2.0 | allowed |
| npm | @swc/types | 0.1.28 | Apache-2.0 | allowed |
| npm | @types/estree | 1.0.9 | MIT | allowed |
| npm | @types/react | 19.3.0 | MIT | allowed |
| npm | @types/react-dom | 19.3.0 | MIT | allowed |
| npm | @vitejs/plugin-react-swc | 4.3.3 | MIT | allowed |
| npm | csstype | 3.2.3 | MIT | allowed |
| npm | esbuild | 0.28.2 | MIT | allowed |
| npm | fdir | 6.5.0 | MIT | allowed |
| npm | fsevents | 2.3.3 | MIT | allowed |
| npm | nanoid | 3.3.20 | MIT | allowed |
| npm | picocolors | 1.1.1 | ISC | allowed |
| npm | picomatch | 4.0.7 | MIT | allowed |
| npm | postcss | 8.5.31 | MIT | allowed |
| npm | react | 19.3.1 | MIT | allowed |
| npm | react-dom | 19.3.1 | MIT | allowed |
| npm | rollup | 4.64.10 | MIT | allowed |
| npm | scheduler | 0.28.0 | MIT | allowed |
| npm | source-map-js | 1.2.2 | BSD-3-Clause | allowed |
| npm | tinyglobby | 0.2.17 | MIT | allowed |
| npm | typescript | 5.9.3 | Apache-2.0 | allowed |
| npm | vite | 7.3.7 | MIT | allowed |
