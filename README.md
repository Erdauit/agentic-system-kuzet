# Qoima Vision

On-prem система промышленной видеоаналитики: падения и лежание, красные зоны, дым и огонь.
Работает полностью офлайн на одном сервере предприятия с 1–2 GPU NVIDIA.
Спецификация — [SPEC.md](SPEC.md), правила разработки — [CLAUDE.md](CLAUDE.md).

## Статус: Фаза 3 — планировщик мощности и бенчмарки

Фаза 3: `qoima bench` (декодирование NVDEC/CPU, модели, VLM на этом сервере), детерминированный
план мощности (FPS камер, VRAM, серая зона арбитра), применение плана, PDF-отчёт, мастер — шаги
2 и 6; образ vision-pipeline для GPU (TensorRT FP16, CUDA, NVDEC).

Фаза 2: hot path lite — GOP-буфер и клипы, детектор движения, детектор людей и поза (ONNX),
трекер, FSM падения/красной зоны/дыма, инциденты с дедупликацией по участку, доставка
at-least-once с эскалацией до сирены, «Оперативная обстановка», самотест и канарейка.
Приёмка падения и дыма на видео ждёт роликов и весов (docs/TODO.md).

Фаза 1: автопоиск камер (ONVIF + сканирование RTSP), Camera Health по §5.9, системные алерты,
вход и роли, мастер (шаги 1–3, 5, 7, 8), симулятор камер `qoima sim`.

Фаза 0 (фундамент):

- uv workspace: отдельный пакет и Dockerfile на каждый сервис (`src/<сервис>/`, `docker/`);
- docker compose: базовый + профили 1 GPU / 2 GPU / engineering (`compose/`);
- Pydantic-контракты всех межсервисных объектов с контрактными тестами и JSON Schema снапшотами;
- конфиги с валидацией: все пороги и SLO с пояснениями (`configs/`);
- Postgres-схема и миграции Alembic (аудит-лог только для добавления, инварианты в CHECK);
- CLI `qoima`: `up`, `down`, `status`, `doctor`, `config validate`;
- structlog (JSON, маскирование секретов), каталог метрик Prometheus, Prometheus + node-exporter + DCGM;
- раздел UI «Состояние системы» (React + Vite + TypeScript strict), раздаётся core-api по TLS;
- CI: ruff, mypy (strict для `shared/`), pytest, лицензионный гейт Python и npm.

Что ещё не реализовано и в какой фазе появится — [docs/TODO.md](docs/TODO.md).
Принятые допущения — [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md).

## Быстрый старт

Сервер предприятия (Linux, драйвер NVIDIA R580+, Docker, nvidia-container-toolkit, uv):

```bash
sudo ./install.sh
```

```bash
qoima up
```

```bash
qoima doctor
```

Разработка без GPU (macOS/Linux, Docker):

```bash
./install.sh --dev
```

```bash
QOIMA_DATA_ROOT=$PWD/.data QOIMA_HTTPS_PORT=8443 .venv-cli/bin/qoima up --no-gpu
```

UI: `https://localhost:8443/` (самоподписанный сертификат из `.secrets/tls.crt`), вход — `admin`,
пароль из `.secrets/admin_initial_password`. Камеры для разработки:

```bash
QOIMA_DATA_ROOT=$PWD/.data .venv-cli/bin/qoima sim up --cameras 4 --no-gpu --build
```

Приёмка Фаз 1–3 на запущенной системе (симуляторы камер и приёмник алертов — `qoima sim up`):

```bash
QOIMA_E2E_URL=https://localhost:8443 QOIMA_DATA_ROOT=$PWD/.data QOIMA_HTTPS_PORT=8443 uv run pytest tests/e2e -m e2e
```

План мощности на сервере: `qoima bench`, затем мастер → шаг 6.

## Разработка

```bash
uv sync
```

```bash
make ci
```

Подробнее — [docs/ENGINEER_GUIDE.md](docs/ENGINEER_GUIDE.md). Установка и эксплуатация —
[docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md). Для оператора — [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md).
