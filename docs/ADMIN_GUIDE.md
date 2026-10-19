# Руководство администратора

Для `site_admin` и инженеров Qoima: установка, запуск, диагностика. Версия для Фазы 0.

## Требования к серверу

| Что | Минимум | Проверяет |
|---|---|---|
| ОС | Linux, рекомендуется Ubuntu 22.04/24.04 LTS | `install.sh`, `qoima doctor` |
| GPU | 1–2 NVIDIA; для локального VLM — ≥ 24 ГБ VRAM на карте | `qoima doctor` |
| Драйвер NVIDIA | с поддержкой CUDA 13.x (ветка R580+) | `qoima doctor` |
| Docker Engine | ≥ 24.0 + плагин compose ≥ 2.20 | `install.sh`, `qoima doctor` |
| nvidia-container-toolkit | runtime `nvidia` зарегистрирован в Docker | `install.sh`, `qoima doctor` |
| Диск под данные | ≥ 200 ГБ свободно в `/var/lib/qoima` | `qoima doctor` |
| Время | chrony, смещение ≤ 50 мс | `qoima doctor` |
| uv | для установки CLI | `install.sh` |

Пороговые значения — `configs/defaults.yaml`, раздел `doctor`.

## Установка

1. Скопируйте каталог поставки на сервер, например в `/opt/qoima`.
2. Запустите установку от root:

   ```bash
   sudo ./install.sh
   ```

   Скрипт проверит окружение, установит CLI `qoima` (`/usr/local/bin/qoima`), сгенерирует
   секреты и самоподписанный TLS-сертификат в `.secrets/`, создаст каталоги данных в
   `/var/lib/qoima`, соберёт образы и запустит `qoima doctor`. Повторный запуск безопасен:
   существующие секреты и данные не меняются.

   Другой каталог данных: `sudo ./install.sh --data-root /data/qoima`.

3. Запустите систему:

   ```bash
   qoima up
   ```

   CLI сам выберет профиль по числу GPU (1 GPU — всё на одной карте; 2 GPU — вторая карта
   отдаётся обучению) и дождётся, пока все сервисы станут healthy.

4. Откройте в браузере `https://<адрес-сервера>/` → раздел «Состояние системы».

> Офлайн-установка из подписанного бандла появится в Фазе 9. Сейчас установке нужен интернет.

## Ежедневные команды

| Команда | Что делает |
|---|---|
| `qoima status` | контейнеры и сводка «Состояние системы» |
| `qoima doctor` | полная самодиагностика с рекомендациями |
| `qoima down` | остановка (данные сохраняются) |
| `qoima up --engineering` | плюс Grafana для инженеров (только с самого сервера: `http://127.0.0.1:3000`) |
| `qoima config validate` | проверка `configs/` после правок |

## Настройка объекта

Не редактируйте `configs/defaults.yaml`. Создайте `configs/site.yaml` только с изменёнными
значениями, например:

```yaml
defaults:
  site:
    name: "Цех №3"
    shifts:
      - {name: "1 смена", start: "07:00", end: "15:00"}
      - {name: "2 смена", start: "15:00", end: "23:00"}
      - {name: "3 смена", start: "23:00", end: "07:00"}
```

После правки: `qoima config validate`, затем `qoima up`.

## Где что лежит

| Путь | Содержимое |
|---|---|
| `.secrets/` | пароли Postgres и Valkey, TLS-сертификат и ключ (каталог 700). Не копируйте в чаты и тикеты |
| `/var/lib/qoima/postgres` | база данных |
| `/var/lib/qoima/{clips,frames,datasets,models}` | клипы, кадры, датасеты, модели |
| `/var/lib/qoima/prometheus` | метрики (30 дней) |
| `configs/` | конфигурация |

Логи сервисов: `docker compose -p qoima logs <сервис>` (JSON, ротация 5 × 50 МБ).

## Типовые проблемы

| Симптом | Что делать |
|---|---|
| `qoima doctor`: «runtime nvidia не зарегистрирован» | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| «драйвер поддерживает CUDA 12.x» | обновить драйвер NVIDIA до ветки R580+ |
| «права каталога .secrets» | `chmod 700 .secrets` |
| `qoima up` не дождался healthy | `qoima status`, затем `docker compose -p qoima logs <сервис>` |
| В UI «Время: нет данных» | ожидаемо до Фазы 1; синхронизацию показывает `qoima doctor` |

Резервное копирование (`qoima backup` / `restore`) и обновления (`qoima update`) — Фаза 9.
