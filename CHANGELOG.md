# Changelog

## [0.2.0] — 2026-10-20 — Фаза 1: камеры, Camera Health, «Состояние системы»

### Добавлено
- Автопоиск камер: адресный и multicast ONVIF WS-Discovery, сканирование RTSP/ONVIF-портов по
  подсетям сервера, опрос ONVIF (модель, профили, адреса потоков) с перебором учётных данных,
  шаблоны URL производителей; подключение найденных устройств (включая каналы NVR) одной
  командой с автоподбором main/substream, снапшотом и базовой линией качества.
- Ручное добавление и импорт CSV/YAML; синхронизация путей MediaMTX с БД.
- Camera Health в vision-pipeline: поток жив, FPS, переподключения, ошибки декодирования,
  замёрзший/чёрный/пересвеченный кадр, резкость, заслонение, сдвиг ракурса (ORB); антидребезг.
- Системные алерты (камеры и компоненты), сторож «пайплайн молчит», метрика SLO обнаружения
  офлайн-камеры; WebSocket-обновления UI; доставка в Telegram/HTTP-реле.
- Вход, сессии, роли, первый администратор, аудит действий; пользователи (API).
- Мастер настройки в UI: шаги 1 (объект), 2 (железо по отчёту doctor), 3 (камеры), 7 (алерты);
  «Состояние системы»: карточки камер, история 24 ч, live (WHEP), SLO-панель, системные алерты.
- Образ `media-base` с LGPL-FFmpeg; PyAV из исходников; симулятор камер `qoima sim`
  (RTSP + ONVIF, неисправности freeze/blur/black/occlude/shift/stop).
- CLI: `qoima sim up|down|fault`, `qoima doctor --report` и проверка камер, `qoima user reset-password`.
- Тесты: юнит (метрики качества, оценщик, ORB, ONVIF против симулятора, сканирование, выбор
  потоков, авторизация, каналы), интеграционные (вход, алерты), e2e-приёмка Фазы 1.

## [0.1.0] — 2026-10-19 — Фаза 0: скелет и фундамент

### Добавлено
- uv workspace из шести пакетов (`shared`, `cli`, `core_api`, `vision_pipeline`, `agents`,
  `workers`), отдельный Dockerfile на сервис; `uv.lock` и `ui/package-lock.json`.
- docker compose: базовый файл и профили `gpu1`, `gpu2`, `engineering`; внутренняя сеть без
  выхода наружу; Docker secrets; healthcheck у каждого сервиса.
- Контракты: Event, Incident, VLMVerdict, ArbiterOutcome, TaskSpec, CameraHealth, CapacityPlan,
  AlertDelivery, SystemAlert, Feedback, ModelManifest, ToolCallAudit и др. — с инвариантами
  fail-open, «человек в контуре», разделением safety/system алертов; JSON Schema снапшоты.
- Конфиги с валидацией: `defaults.yaml` (пороги и SLO с пояснениями), сценарии, шаблоны URL
  камер, алерты; переопределения объекта через `site.yaml`.
- Postgres-схема и миграция `0001`: пользователи, неизменяемый аудит-лог, камеры, зоны, события,
  инциденты, фидбек, реестр моделей.
- core-api: `/api/v1/healthz`, `/api/v1/health` («Состояние системы»), `/metrics`, раздача UI, TLS.
- CLI `qoima`: `up`, `down`, `status`, `doctor`, `config validate`; команды следующих фаз
  сообщают, когда появятся.
- `install.sh`: проверки окружения, CLI, секреты и TLS, каталоги данных, сборка образов, doctor.
- UI: каркас и раздел «Состояние системы» (ru/kk).
- structlog с маскированием секретов, каталог метрик SPEC.md §6.2, Prometheus + node-exporter + DCGM.
- CI: ruff, mypy (strict для `shared/`), pytest с Postgres и Valkey, лицензионный гейт Python и
  npm, сборка UI, `docker compose config`, shellcheck.
- Документация: ADMIN/ENGINEER/OPERATOR guide, ASSUMPTIONS, TODO, PRIVACY, DATA_LICENSES.
