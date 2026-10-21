# Стенд на Windows (WSL2)

Qoima Vision на объекте работает на Ubuntu LTS. На машине с Windows и GPU NVIDIA систему можно
поднять для проверки и демонстрации — внутри WSL2 (Linux в Windows). Всё, что ниже, — про такой
стенд; при переезде на Linux ставьте систему заново (раздел в конце).

> Что проверено: проверки установщика и `qoima doctor` для WSL покрыты тестами; сам запуск
> на реальной машине с Windows и GPU мы не проводили — пришлите вывод `qoima doctor`, если
> что-то пойдёт не так.

## Что нужно

| Что | Требование |
|---|---|
| Windows | 11 22H2 и новее (нужен режим сети `mirrored`); на Windows 10 — только режим NAT |
| GPU | NVIDIA с драйвером для Windows ветки R580+ (CUDA 13). **Внутри Ubuntu драйвер не ставится** |
| Память | ≥ 32 ГБ на машине; WSL выделить 24 ГБ (по умолчанию он берёт только половину) |
| Диск | ≥ 80 ГБ свободно на диске Windows, где лежит WSL (обычно C:) — образы ≈ 30 ГБ, кэш сборки ≈ 15 ГБ, модели и клипы проверки. Подробно — [ADMIN_GUIDE.md](ADMIN_GUIDE.md#сколько-места-занимает-система) |

Виртуальный диск WSL сам растёт на диске Windows и сам не уменьшается: смотрите на свободное
место именно на C:, а не внутри Ubuntu — `qoima doctor` показывает оба.

## Установка

1. **WSL и Ubuntu.** PowerShell от администратора:

   ```powershell
   wsl --install -d Ubuntu-24.04
   ```

   ```powershell
   wsl --update
   ```

2. **Настройки WSL.** Создайте файл `%UserProfile%\.wslconfig`:

   ```ini
   [wsl2]
   networkingMode=mirrored
   memory=24GB
   swap=8GB
   ```

   `mirrored` — WSL в той же сети, что и Windows: автопоиск видит камеры, а UI открывается с
   других компьютеров. Затем перезапустите WSL:

   ```powershell
   wsl --shutdown
   ```

3. **systemd в Ubuntu** (без него не стартует Docker и таймер самодиагностики). В Ubuntu в
   файле `/etc/wsl.conf` должно быть:

   ```ini
   [boot]
   systemd=true
   ```

   После правки — снова `wsl --shutdown` в PowerShell.

4. **Docker Engine и GPU в контейнерах** (внутри Ubuntu): Docker Engine по инструкции
   docs.docker.com/engine/install/ubuntu, затем nvidia-container-toolkit по инструкции NVIDIA и:

   ```bash
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   ```

   ```bash
   sudo usermod -aG docker "$USER"
   ```

   (после `usermod` откройте Ubuntu заново). Проверка — контейнер видит видеокарту:

   ```bash
   docker run --rm --gpus all postgres:16.15-bookworm nvidia-smi -L
   ```

   Docker Desktop вместо Docker Engine тоже подходит: установщик проверяет GPU «делом», а не по
   названию runtime. Но основной вариант — Docker Engine внутри Ubuntu.

5. **Остальное**: chrony, git, openssl и uv:

   ```bash
   sudo apt install -y chrony git openssl
   ```

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

6. **Репозиторий — внутри Ubuntu, не на диске Windows.** Клонируйте git-ом из Ubuntu (не Git for
   Windows): на `/mnt/c/...` не работают права Linux на секреты, а сборка в разы медленнее —
   установщик такой каталог не примет.

   ```bash
   cd ~ && git clone https://github.com/Erdauit/agentic-system-kuzet.git && cd agentic-system-kuzet
   ```

7. **Установка и запуск** — как на Linux:

   ```bash
   sudo ./install.sh
   ```

   ```bash
   qoima doctor
   ```

   ```bash
   qoima up --build
   ```

   На WSL `qoima up` не запускает мониторинг GPU DCGM (в WSL он недоступен) — метрик видеокарты в
   Prometheus не будет, остальное работает. `qoima doctor` помечает ОС как «стенд» (WARN) и
   проверяет сеть WSL, диск Windows, память и systemd.

## Доступ к интерфейсу

- С этого компьютера: `https://localhost/`.
- С других компьютеров сети: нужен `networkingMode=mirrored` и разрешение входящих подключений.
  PowerShell от администратора:

  ```powershell
  New-NetFirewallRule -DisplayName "Qoima Vision HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
  ```

  Если всё равно не открывается — в режиме `mirrored` входящие соединения дополнительно
  фильтрует брандмауэр Hyper-V для WSL:

  ```powershell
  Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
  ```

## Чтобы стенд работал без открытого окна Ubuntu

Windows останавливает дистрибутив WSL, когда в нём не остаётся процессов, запущенных из Windows
(закрыли последнее окно Ubuntu) — вместе с Docker и системой. Пока идёт проверка, держите окно
Ubuntu открытым; для работы без окна создайте задачу при входе в Windows (PowerShell):

```powershell
schtasks /create /tn "Qoima WSL" /tr "wsl.exe -d Ubuntu-24.04 -e sleep infinity" /sc onlogon
```

Контейнеры Qoima перезапускаются сами (`restart: unless-stopped`), как только Docker в Ubuntu
снова запущен. Отключите сон Windows в настройках питания: во сне камеры не анализируются, а после
пробуждения у WSL бывает сдвиг часов (`qoima doctor` → «Время»; лечится `sudo hwclock -s`).

## Ограничения стенда

- Метрики GPU (DCGM) недоступны; загрузка видеокарты видна только в `nvidia-smi`.
- Аппаратное декодирование NVDEC в WSL зависит от драйвера: если не работает, видео
  декодируется на CPU — `qoima bench` и «Состояние системы» это покажут.
- Live-просмотр (WebRTC) может не работать в режиме NAT; в `mirrored` — проверьте на месте.
- Замеры `qoima bench` на Windows не переносятся на Linux: план мощности для объекта считается
  по бенчмарку на самом сервере объекта.

## Переезд на Linux

На сервере объекта поставьте Ubuntu 22.04/24.04 LTS и выполните установку по
[ADMIN_GUIDE.md](ADMIN_GUIDE.md): `sudo ./install.sh` → `qoima doctor` → `qoima up` → мастер
(камеры найдутся заново) → `qoima bench` → план мощности (шаг 6). Перенос данных со стенда
(резервная копия и восстановление) — Фаза 9; для стенда проверки он не нужен.
