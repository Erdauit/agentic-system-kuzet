# Руководство инженера

Архитектура и правила расширения. Принципы и порядок работы — [CLAUDE.md](../CLAUDE.md),
полная спецификация — [SPEC.md](../SPEC.md).

## Архитектура (Фаза 0)

```text
            браузер ──TLS──▶ core-api ──▶ postgres (схема, аудит)
                               │   └────▶ valkey (очереди, кэш)
                               │   └────▶ /readyz фоновых сервисов, qdrant, mediamtx, triton
 камеры ──RTSP──▶ mediamtx ◀── vision-pipeline (Фаза 2: hot path)
                               agents (Фазы 4–8), worker-gpu (Фазы 6–7)
 prometheus ◀── /metrics всех сервисов, node-exporter, dcgm-exporter (GPU)
```

- Сеть `backend` — `internal: true`: у сервисов нет выхода наружу. В сети `edge` только
  `core-api` (UI/API) и `mediamtx` (камеры в сети завода). vision-pipeline к камерам — только
  через MediaMTX.
- `core-api` при старте применяет миграции, затем поднимает uvicorn с TLS.
- Фоновые сервисы используют `shared.telemetry.runner`: `/healthz` (жив), `/readyz` (готов:
  зависимости + `capabilities`), `/metrics`. Пока `capabilities` пуст, core-api показывает
  сервис жёлтым — так UI не выдаёт нереализованное за работающее.

## Репозиторий

| Каталог | Что внутри |
|---|---|
| `src/shared/schemas/` | Pydantic-контракты (`CONTRACTS`) и экспорт JSON Schema |
| `src/shared/config/` | модели и загрузчик `configs/*.yaml` (+ `site.yaml`) |
| `src/shared/storage/` | `BlobStore` (LocalFS), Postgres (asyncpg), Valkey, секреты |
| `src/shared/telemetry/` | structlog, каталог метрик, служебный HTTP, healthcheck-проба |
| `src/shared/licenses/` | `policy.yaml`, `components.yaml`, гейт |
| `src/core_api/` | FastAPI, `system_health.py`, `db/` (модели, миграции) |
| `src/cli/` | `qoima`: `compose.py` (профили), `doctor.py` (проверки) |
| `tests/contract/` | примеры и снапшоты контрактов |
| `tests/unit/`, `tests/integration/` | юнит-тесты; интеграционные — с реальными Postgres/Valkey |

## Как добавить…

**Контракт.** Модель в `src/shared/schemas/<модуль>.py` от `Contract` → регистрация в
`CONTRACTS` → пример в `tests/contract/examples.py` → `make schemas` → инварианты — отдельными
тестами в `tests/contract/test_contracts.py`.

**Порог или SLO.** Поле в `src/shared/config/models.py` (без значения по умолчанию) → значение
с комментарием «почему» в `configs/defaults.yaml` (или в сценарии) → тест на валидацию.

**Зависимость.** `pyproject.toml` нужного пакета `src/<сервис>/` → `uv lock` → `make license-gate`.
Лицензия вне белого списка — только через ревью: запись в `policy.yaml: reviewed` и в
`docs/DATA_LICENSES.md`.

**Таблицу БД.** Модель в `src/core_api/db/models.py` (перечисления — text + `enum_check`) →
`alembic -c src/core_api/db/alembic.ini revision --autogenerate --rev-id 0002 -m "..."` при
запущенном Postgres и `QOIMA_DATABASE_URL` → проверить сгенерированное → `make test-integration`
(тест сверяет ORM и миграции).

**Метрику.** Сначала в каталог `src/shared/telemetry/metrics.py`, затем `create_metric(name, registry)`
в сервисе. Метрика регистрируется только когда сервис её реально измеряет.

**Docker-образ.** Закреплённый тег + запись в `src/shared/licenses/components.yaml` с лицензией.

## Тесты

```bash
make test
```

```bash
docker run -d --rm --name qoima-test-pg -e POSTGRES_USER=qoima -e POSTGRES_PASSWORD=test -e POSTGRES_DB=qoima -p 127.0.0.1:55432:5432 postgres:16.15-bookworm
```

```bash
docker run -d --rm --name qoima-test-valkey -p 127.0.0.1:56379:6379 valkey/valkey:8.1.10
```

```bash
QOIMA_TEST_DATABASE_URL=postgresql+asyncpg://qoima:test@127.0.0.1:55432/qoima QOIMA_TEST_VALKEY_URL=valkey://127.0.0.1:56379/0 make test-integration
```

Приёмка Фазы 0 на машине с GPU: `sudo ./install.sh && qoima up && qoima status && qoima doctor` —
все сервисы healthy, doctor без ошибок.
