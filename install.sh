#!/usr/bin/env bash
# Qoima Vision — установка на сервер предприятия (SPEC.md §3.1).
#
#   sudo ./install.sh                 # установка из исходников этого каталога (нужен интернет)
#   sudo ./install.sh --bundle FILE   # офлайн-бандл (Фаза 9, пока не реализовано)
#   ./install.sh --dev                # разработка без root и без GPU: данные в ./.data
#
# Что делает:
#   1. проверяет ОС, Docker, Compose, драйвер NVIDIA и nvidia-container-toolkit;
#   2. ставит CLI `qoima` (uv) и ссылку /usr/local/bin/qoima;
#   3. генерирует секреты в .secrets/ (никогда не перезаписывает существующие) и TLS-сертификат;
#   4. создаёт каталоги данных с правильными владельцами;
#   5. собирает образы Qoima и скачивает сторонние образы;
#   6. запускает `qoima doctor` и подсказывает следующий шаг — `qoima up`.
#
# Повторный запуск безопасен: существующие секреты, сертификат и данные не трогаются.

set -euo pipefail

QOIMA_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT="/var/lib/qoima"
SECRETS_DIR="${QOIMA_HOME}/.secrets"
BUNDLE=""
DEV=0

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle) BUNDLE="${2:?--bundle требует путь к файлу}"; shift 2 ;;
        --data-root) DATA_ROOT="${2:?--data-root требует путь}"; shift 2 ;;
        --dev) DEV=1; DATA_ROOT="${QOIMA_HOME}/.data"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; usage; exit 2 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
fail() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 0. офлайн-бандл
if [[ -n "${BUNDLE}" ]]; then
    # Подписанные офлайн-бандлы и проверка подписи — Фаза 9 (docs/TODO.md). Устанавливать
    # неподписанные образы мы не будем: это обход требования SPEC.md §10.
    fail "установка из офлайн-бандла реализуется в Фазе 9. Сейчас поддерживается только установка из исходников с доступом в интернет."
fi

# ---------------------------------------------------------------- 1. предварительные проверки
info "Проверка окружения"
if [[ "${DEV}" -eq 0 ]]; then
    [[ "$(uname -s)" == "Linux" ]] || fail "боевая установка поддерживается только на Linux (для разработки: --dev)"
    [[ "${EUID}" -eq 0 ]] || fail "запустите от root: sudo ./install.sh"
fi
command -v docker >/dev/null 2>&1 || fail "Docker не найден. Установите Docker Engine: https://docs.docker.com/engine/install/"
docker info >/dev/null 2>&1 || fail "Docker не запущен или нет прав на docker.sock"
docker compose version >/dev/null 2>&1 || fail "плагин docker compose v2 не найден (пакет docker-compose-plugin)"
command -v openssl >/dev/null 2>&1 || fail "openssl не найден — нужен для генерации TLS-сертификата"
if [[ "${DEV}" -eq 0 ]]; then
    command -v nvidia-smi >/dev/null 2>&1 || fail "драйвер NVIDIA не найден (nvidia-smi). Установите драйвер ветки R580+"
    docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"' \
        || fail "nvidia-container-toolkit не настроен: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
    if ! command -v chronyc >/dev/null 2>&1; then
        warn "chrony не найден — синхронизацию времени нужно настроить (SPEC.md §3.1), проверит qoima doctor"
    fi
fi

# ---------------------------------------------------------------- 2. CLI
info "Установка CLI qoima"
UV_BIN="$(command -v uv || true)"
[[ -n "${UV_BIN}" ]] || fail "uv не найден. Установите uv (https://docs.astral.sh/uv/) — в офлайн-бандле Фазы 9 он будет в комплекте"
# Отдельное окружение .venv-cli: на хосте нужен только CLI, а .venv разработчика не трогаем.
CLI_ENV="${QOIMA_HOME}/.venv-cli"
(cd "${QOIMA_HOME}" && UV_PROJECT_ENVIRONMENT="${CLI_ENV}" "${UV_BIN}" sync --frozen --no-dev --package qoima-cli)
QOIMA_BIN="${CLI_ENV}/bin/qoima"
[[ -x "${QOIMA_BIN}" ]] || fail "CLI не установился: нет ${QOIMA_BIN}"

if [[ "${DEV}" -eq 0 ]]; then
    cat > /usr/local/bin/qoima <<EOF
#!/usr/bin/env bash
export QOIMA_HOME="${QOIMA_HOME}"
export QOIMA_DATA_ROOT="${DATA_ROOT}"
exec "${QOIMA_BIN}" "\$@"
EOF
    chmod 755 /usr/local/bin/qoima
    info "CLI: /usr/local/bin/qoima"
fi

# ---------------------------------------------------------------- 3. секреты и TLS
info "Секреты: ${SECRETS_DIR}"
umask 077
mkdir -p "${SECRETS_DIR}"
# Каталог 700 закрывает секреты от других пользователей хоста. Файлы внутри — 644: Docker
# Compose (без swarm) монтирует secrets с правами хоста, а процессы контейнеров работают под
# своими uid (postgres — 999, сервисы Qoima — 10001). См. docs/ASSUMPTIONS.md.
chmod 700 "${SECRETS_DIR}"

gen_secret() {
    local name="$1"
    if [[ -s "${SECRETS_DIR}/${name}" ]]; then
        echo "   ${name}: уже есть, не меняю"
        return
    fi
    openssl rand -base64 48 | tr -d '/+=\n' | head -c 40 > "${SECRETS_DIR}/${name}"
    echo "   ${name}: сгенерирован"
}
gen_secret postgres_password
gen_secret valkey_password

if [[ -s "${SECRETS_DIR}/tls.crt" && -s "${SECRETS_DIR}/tls.key" ]]; then
    echo "   tls.crt/tls.key: уже есть, не меняю"
else
    HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
    HOST_SHORT="$(hostname -s 2>/dev/null || hostname)"
    # Самоподписанный сертификат по умолчанию (SPEC.md §3.1). 825 дней — максимум, который
    # принимают современные браузеры для серверных сертификатов.
    openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
        -keyout "${SECRETS_DIR}/tls.key" -out "${SECRETS_DIR}/tls.crt" \
        -subj "/CN=${HOST_FQDN}/O=Qoima Vision" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:${HOST_SHORT},DNS:${HOST_FQDN}" \
        >/dev/null 2>&1 || fail "не удалось сгенерировать TLS-сертификат (нужен OpenSSL ≥ 1.1.1)"
    echo "   tls.crt/tls.key: самоподписанный сертификат для ${HOST_FQDN}"
fi
chmod 644 "${SECRETS_DIR}"/*
umask 022

# ---------------------------------------------------------------- 4. каталоги данных
info "Каталоги данных: ${DATA_ROOT}"
# Владельцы — uid процессов в контейнерах (src/cli/paths.py: DATA_DIRS).
make_dir() {
    local sub="$1" owner="$2"
    mkdir -p "${DATA_ROOT}/${sub}"
    if [[ "${DEV}" -eq 0 ]]; then
        chown "${owner}" "${DATA_ROOT}/${sub}"
    fi
}
make_dir clips 10001:10001
make_dir frames 10001:10001
make_dir datasets 10001:10001
make_dir models 10001:10001
make_dir models/triton 10001:10001
make_dir postgres 999:999
make_dir valkey 999:999
make_dir qdrant 0:0
make_dir prometheus 65534:65534
make_dir grafana 472:0

# ---------------------------------------------------------------- 5. образы
export QOIMA_HOME QOIMA_DATA_ROOT="${DATA_ROOT}" QOIMA_SECRETS_DIR="${SECRETS_DIR}"
COMPOSE=(docker compose --project-name qoima --file "${QOIMA_HOME}/compose/compose.yml")
if [[ "${DEV}" -eq 0 ]]; then
    COMPOSE+=(--file "${QOIMA_HOME}/compose/compose.gpu1.yml")
fi
info "Загрузка сторонних образов"
"${COMPOSE[@]}" pull --ignore-buildable --quiet
info "Сборка образов Qoima"
"${COMPOSE[@]}" build

# ---------------------------------------------------------------- 6. самодиагностика
info "Самодиагностика (qoima doctor)"
DOCTOR_ARGS=()
[[ "${DEV}" -eq 1 ]] && DOCTOR_ARGS+=(--no-gpu)
if QOIMA_HOME="${QOIMA_HOME}" QOIMA_DATA_ROOT="${DATA_ROOT}" "${QOIMA_BIN}" doctor "${DOCTOR_ARGS[@]}"; then
    info "Установка завершена. Запуск: $([[ "${DEV}" -eq 1 ]] && echo "QOIMA_DATA_ROOT=${DATA_ROOT} .venv-cli/bin/qoima up --no-gpu" || echo "qoima up")"
else
    warn "doctor нашёл проблемы — исправьте их по рекомендациям и запустите qoima doctor снова"
    exit 1
fi
