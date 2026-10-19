# Не реализовано (по фазам)

Каждый пункт здесь — то, что в коде явно помечено как «не реализовано» (`NotImplementedError`,
пустой `capabilities`, статус `not_deployed` в health). Ничего из этого не имитируется.

## Фаза 1 — камеры, Camera Health, health-раздел UI
- `qoima sim` — виртуальные RTSP-камеры из `samples/` (нужны клипы от команды).
- Мастер настройки, шаги 1–3 и 7; ffprobe-зондирование, поиск substream по
  `configs/camera_url_templates.yaml` и ONVIF; снапшоты и базовая линия качества.
- Camera Health в vision-pipeline (все проверки SPEC.md §5.9) и системные алерты; таблица
  `system_alerts`.
- Аутентификация пользователей, RBAC по ролям, запись действий в `audit_log`.
- Health: проверка синхронизации времени из UI (сейчас — только `qoima doctor`).
- `qoima doctor`: доступность и тест декодирования каждой камеры.
- Публикация портов WebRTC MediaMTX для live-просмотра.

## Фаза 2 — hot path и алерты
- Декодирование substream (NVDEC), GOP-буфер, детектор движения, RF-DETR + поза, трекер, гомография.
- FallFSM, RedZoneFSM, SmokeFireFSM; инциденты, дедупликация, cooldown, эскалация.
- Каналы алертов (web, Telegram, HTTP-реле, Modbus); таблица `alert_deliveries`; health «каналы».
- Клипы, фидбек с кодами причин, раздел «Оперативная обстановка».
- CUDA/TensorRT/PyAV в образе vision-pipeline.

## Фаза 3 — планировщик мощности
- `qoima bench`, `CapacityPlan`, PDF-отчёт, шаги 2 и 6 мастера.
- Выбор vLLM vs SGLang по бенчмарку.

## Фаза 4 — LLM и VLM-арбитр
- Сервис `llm` в compose (сейчас health: «не развёрнуто»), клиент с приоритетами и constrained
  decoding, арбитр, режимы shadow/assist. Контракты `VLMVerdict`/`ArbiterOutcome` уже есть.
- `qoima doctor`: smoke-тест LLM.

## Фаза 5 — чат, Setup Assistant, Ops Agent
- Раздел «Чат», инструменты SPEC.md §8.2 с RBAC и аудитом (`ToolCallAudit`), `qoima eval agents`.

## Фаза 6 — data flywheel и разметка
- Клиент Qdrant в `shared/storage`, эмбеддинги, поиск по архиву, раздел «Разметка», golden sets.

## Фаза 7 — AutoML
- Плейбуки агентов, граф AutoML (LangGraph), Triton hot-load, shadow, «Выкатить / Откатить».
- worker-gpu: потребители задач, обучение, экспорт TensorRT.

## Фаза 8 — Feedback Analyst

## Фаза 9 — масштаб и закалка
- Backend `deepstream`; `qoima update` (подписанные офлайн-бандлы, откат), `install.sh --bundle`.
- `qoima backup` / `qoima restore`, `qoima support-bundle`, нагрузочный тест.
