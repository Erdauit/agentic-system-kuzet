# Не реализовано (по фазам)

Каждый пункт здесь — то, что в коде явно помечено как «не реализовано» (`NotImplementedError`,
пустой `capabilities`, статус `not_deployed` в health). Ничего из этого не имитируется.

## Фаза 1 — сделано, остались хвосты
- Live-просмотр: сигнализация WHEP и сессия WebRTC проверены (MediaMTX читает поток), но
  картинка в браузере на macOS/Docker Desktop не пошла из-за UDP-NAT Docker Desktop; визуально
  проверить на Linux-сервере объекта.
- Тексты проверок и алертов приходят с сервера только на русском — локализация kk.
- Привязка к реальным камерам: шаблоны URL производителей (`verified: false`) и ONVIF проверены
  только на симуляторе — подтвердить на камерах объекта.
- Мастер, шаг 1: редактор графика смен — текстовый; удобный редактор — Фаза 9.

## Фаза 2 — hot path и алерты
- Декодирование substream (NVDEC), GOP-буфер, детектор движения, RF-DETR + поза, трекер, гомография.
- FallFSM, RedZoneFSM, SmokeFireFSM; инциденты, дедупликация, cooldown, эскалация.
- Маршрутизация инцидентов по каналам, эскалация, сирена по Modbus, очередь at-least-once
  (`alert_deliveries`). Адаптеры web/Telegram/HTTP-реле и тестовая отправка уже есть (Фаза 1).
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
- Раздел «Настройки» UI (пользователи — пока только API `/api/v1/users`).
- `qoima backup` / `qoima restore`, `qoima support-bundle`, нагрузочный тест.
