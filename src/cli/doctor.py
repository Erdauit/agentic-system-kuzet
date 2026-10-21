"""`qoima doctor` — самодиагностика сервера (SPEC.md §6.3).

Фаза 0: ОС, драйвер NVIDIA и поддерживаемая версия CUDA, VRAM, Docker и Compose,
nvidia-container-toolkit, свободное место, синхронизация времени, секреты, конфиги и состояние
контейнеров. Проверки камер, моделей, LLM и E2E-канарейки добавляются в своих фазах (docs/TODO.md).

Каждая проверка — чистая функция от вывода команд: так её можно протестировать без железа.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cli.compose import ComposePlan, parse_ps
from cli.hostenv import docker_gpu_works, nvidia_smi, windows_drive, wsl_networking_mode
from cli.paths import REQUIRED_SECRETS
from cli.runner import Runner
from shared.config import ConfigError, QoimaConfig, load_config
from shared.schemas import DoctorGpu, DoctorLevel, DoctorOutcome, DoctorReport


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Outcome:
    name: str
    level: Level
    detail: str
    recommendation: str = ""


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"\d+(?:\.\d+)*", text)
    if not m:
        raise ValueError(f"не удалось разобрать версию: {text!r}")
    return tuple(int(x) for x in m.group(0).split("."))


def version_at_least(actual: str, minimum: str) -> bool:
    a, b = parse_version(actual), parse_version(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


# ---------------------------------------------------------------- ОС


def check_os(system: str, os_release: str | None, wsl: bool = False) -> Outcome:
    if system == "Linux" and wsl:
        return Outcome(
            "ОС",
            Level.WARN,
            "WSL2 (Linux под Windows): подходит для проверки и демонстрации, не для объекта",
            "На объекте — Ubuntu 22.04/24.04 LTS без Windows (docs/WINDOWS.md)",
        )
    if system != "Linux":
        return Outcome(
            "ОС",
            Level.WARN,
            f"{system}: боевой режим поддерживается только на Linux",
            "Для объекта используйте Ubuntu 22.04/24.04 LTS; на этой машине доступен режим разработки",
        )
    pretty = "Linux"
    if os_release:
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', os_release, re.MULTILINE)
        pretty = m.group(1) if m else pretty
    return Outcome("ОС", Level.OK, pretty)


# ---------------------------------------------------------------- NVIDIA


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    driver: str
    vram_mb: int


def parse_gpus(csv_output: str) -> list[Gpu]:
    gpus: list[Gpu] = []
    for line in csv_output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        idx, name, driver, mem = parts
        gpus.append(Gpu(int(idx), name, driver, int(float(mem))))
    return gpus


def parse_cuda_version(nvidia_smi_header: str) -> str | None:
    m = re.search(r"CUDA Version:\s*([\d.]+)", nvidia_smi_header)
    return m.group(1) if m else None


def check_nvidia(runner: Runner, cfg: QoimaConfig) -> tuple[list[Outcome], list[Gpu]]:
    query = runner(
        [
            nvidia_smi(),
            "--query-gpu=index,name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if not query.ok:
        return [
            Outcome(
                "Драйвер NVIDIA",
                Level.FAIL,
                (query.stderr or query.stdout).strip() or "nvidia-smi недоступен",
                "Установите драйвер NVIDIA (ветка R580+) и перезагрузите сервер",
            )
        ], []
    gpus = parse_gpus(query.stdout)
    if not gpus:
        return [
            Outcome(
                "Драйвер NVIDIA",
                Level.FAIL,
                "nvidia-smi не вернул ни одного GPU",
                "Проверьте установку GPU",
            )
        ], []

    outcomes = [Outcome("Драйвер NVIDIA", Level.OK, f"{gpus[0].driver}, GPU: {len(gpus)}")]

    min_cuda = cfg.defaults.doctor.min_driver_cuda_version
    cuda = parse_cuda_version(runner(["nvidia-smi"]).stdout)
    if cuda is None:
        outcomes.append(
            Outcome("CUDA (драйвер)", Level.WARN, "не удалось определить версию CUDA из nvidia-smi")
        )
    elif not version_at_least(cuda, min_cuda):
        outcomes.append(
            Outcome(
                "CUDA (драйвер)",
                Level.FAIL,
                f"драйвер поддерживает CUDA {cuda}, нужно ≥ {min_cuda}",
                "Обновите драйвер NVIDIA до ветки с поддержкой CUDA 13 (R580+)",
            )
        )
    else:
        outcomes.append(Outcome("CUDA (драйвер)", Level.OK, f"CUDA {cuda} (нужно ≥ {min_cuda})"))

    min_vram_mb = cfg.defaults.vlm.min_vram_gb * 1024
    for gpu in gpus:
        if gpu.vram_mb >= min_vram_mb:
            outcomes.append(
                Outcome(f"GPU{gpu.index}", Level.OK, f"{gpu.name}, {gpu.vram_mb // 1024} ГБ VRAM")
            )
        else:
            outcomes.append(
                Outcome(
                    f"GPU{gpu.index}",
                    Level.WARN,
                    f"{gpu.name}, {gpu.vram_mb // 1024} ГБ VRAM < {cfg.defaults.vlm.min_vram_gb:g} ГБ",
                    "На этой карте нельзя держать локальный VLM: алерты без арбитража, чат ограничен (SPEC.md §4.1)",
                )
            )
    return outcomes, gpus


# ---------------------------------------------------------------- Docker


def check_docker(runner: Runner, cfg: QoimaConfig) -> list[Outcome]:
    server = runner(["docker", "version", "--format", "{{.Server.Version}}"])
    if not server.ok:
        return [
            Outcome(
                "Docker",
                Level.FAIL,
                (server.stderr or "docker недоступен").strip().splitlines()[-1],
                "Установите Docker Engine и убедитесь, что служба запущена (systemctl status docker)",
            )
        ]
    outcomes: list[Outcome] = []
    version = server.stdout.strip()
    min_docker = cfg.defaults.doctor.min_docker_version
    if version_at_least(version, min_docker):
        outcomes.append(Outcome("Docker", Level.OK, version))
    else:
        outcomes.append(
            Outcome("Docker", Level.FAIL, f"{version} < {min_docker}", "Обновите Docker Engine")
        )

    compose = runner(["docker", "compose", "version", "--short"])
    min_compose = cfg.defaults.doctor.min_compose_version
    if not compose.ok:
        outcomes.append(
            Outcome(
                "Docker Compose",
                Level.FAIL,
                "плагин compose v2 не найден",
                "Установите docker-compose-plugin",
            )
        )
    elif not version_at_least(compose.stdout.strip(), min_compose):
        outcomes.append(
            Outcome(
                "Docker Compose",
                Level.FAIL,
                f"{compose.stdout.strip()} < {min_compose}",
                "Обновите docker-compose-plugin",
            )
        )
    else:
        outcomes.append(Outcome("Docker Compose", Level.OK, compose.stdout.strip()))
    return outcomes


def check_nvidia_runtime(runner: Runner) -> Outcome:
    info = runner(["docker", "info", "--format", "{{json .Runtimes}}"])
    if not info.ok:
        return Outcome("nvidia-container-toolkit", Level.SKIP, "Docker недоступен")
    if '"nvidia"' in info.stdout:
        return Outcome(
            "nvidia-container-toolkit", Level.OK, "runtime nvidia зарегистрирован в Docker"
        )
    # Docker Desktop (Windows) и CDI дают GPU контейнерам без runtime «nvidia» — проверяем делом.
    works, detail = docker_gpu_works(runner)
    if works:
        return Outcome(
            "nvidia-container-toolkit", Level.OK, f"GPU доступен в контейнерах ({detail})"
        )
    return Outcome(
        "nvidia-container-toolkit",
        Level.FAIL,
        f"контейнер не видит GPU: {detail or 'runtime nvidia не зарегистрирован в Docker'}",
        "Linux/WSL с Docker Engine: установите nvidia-container-toolkit, затем "
        "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker. "
        "Docker Desktop: Settings → Resources → WSL integration и обновите драйвер NVIDIA в Windows",
    )


def check_wsl(
    runner: Runner, min_free_gb: float, min_memory_gb: float, meminfo: str | None
) -> list[Outcome]:
    """Проверки стенда WSL2: сеть для автопоиска, диск Windows под виртуальным диском, память, systemd."""
    out: list[Outcome] = []
    mode = wsl_networking_mode(runner)
    if mode == "mirrored":
        out.append(Outcome("WSL: сеть", Level.OK, "mirrored — автопоиск видит сеть Windows"))
    else:
        out.append(
            Outcome(
                "WSL: сеть",
                Level.WARN,
                f"режим сети: {mode or 'неизвестен (NAT по умолчанию)'} — автопоиск видит только сеть WSL",
                "В %UserProfile%\\.wslconfig: [wsl2] networkingMode=mirrored, затем wsl --shutdown; "
                "или укажите подсеть камер в мастере (шаг 1)",
            )
        )
    drive = windows_drive("c")
    if drive is not None:
        usage = shutil.disk_usage(drive)
        free_gb = usage.free / 1024**3
        detail = f"диск Windows C: свободно {free_gb:.0f} ГБ (на нём растёт виртуальный диск WSL)"
        if free_gb < min_free_gb:
            out.append(
                Outcome(
                    "WSL: диск Windows",
                    Level.FAIL,
                    detail,
                    f"Нужно ≥ {min_free_gb:g} ГБ на диске с WSL: освободите место или перенесите дистрибутив "
                    "(wsl --export / wsl --import на другой диск)",
                )
            )
        else:
            out.append(Outcome("WSL: диск Windows", Level.OK, detail))
    total_gb = None
    if meminfo:
        m = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo, re.MULTILINE)
        total_gb = int(m.group(1)) / 1024**2 if m else None
    if total_gb is not None:
        if total_gb < min_memory_gb:
            out.append(
                Outcome(
                    "WSL: память",
                    Level.WARN,
                    f"WSL видит {total_gb:.0f} ГБ ОЗУ < {min_memory_gb:g} ГБ",
                    "По умолчанию WSL берёт половину памяти Windows: в .wslconfig задайте [wsl2] memory=…GB",
                )
            )
        else:
            out.append(Outcome("WSL: память", Level.OK, f"WSL видит {total_gb:.0f} ГБ ОЗУ"))
    if not Path("/run/systemd/system").is_dir():
        out.append(
            Outcome(
                "WSL: systemd",
                Level.WARN,
                "systemd не запущен — Docker и таймер qoima doctor не стартуют сами",
                "В /etc/wsl.conf: [boot] systemd=true, затем в Windows: wsl --shutdown",
            )
        )
    return out


# ---------------------------------------------------------------- диск и время


def _existing_parent(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def check_disk(path: Path, min_free_gb: float) -> Outcome:
    probe = _existing_parent(path)
    usage = shutil.disk_usage(probe)
    free_gb = usage.free / 1024**3
    detail = f"{path}: свободно {free_gb:.0f} ГБ из {usage.total / 1024**3:.0f} ГБ"
    if free_gb < min_free_gb:
        return Outcome(
            "Диск",
            Level.FAIL,
            detail,
            f"Нужно минимум {min_free_gb:g} ГБ свободного места под клипы, датасеты и модели",
        )
    return Outcome("Диск", Level.OK, detail)


_CHRONY_OFFSET = re.compile(r"System time\s*:\s*([\d.]+)\s+seconds\s+(fast|slow)")
_CHRONY_LEAP = re.compile(r"Leap status\s*:\s*(.+)")


def parse_chrony(output: str) -> tuple[float | None, str | None]:
    """Смещение (мс) и Leap status из `chronyc -n tracking`."""
    offset = _CHRONY_OFFSET.search(output)
    leap = _CHRONY_LEAP.search(output)
    return (
        float(offset.group(1)) * 1000 if offset else None,
        leap.group(1).strip() if leap else None,
    )


def check_time(runner: Runner, max_offset_ms: float, wsl: bool = False) -> Outcome:
    outcome = _check_time(runner, max_offset_ms)
    if wsl and outcome.level is Level.FAIL:
        # Стенд WSL2 берёт время у Windows; после сна Windows бывает сдвиг. Для проверки это не
        # блокер, на объекте (Linux) — блокер.
        return Outcome(
            outcome.name,
            Level.WARN,
            outcome.detail,
            "WSL2: время берётся у Windows; после сна выполните sudo hwclock -s или поставьте chrony",
        )
    return outcome


def _check_time(runner: Runner, max_offset_ms: float) -> Outcome:
    chrony = runner(["chronyc", "-n", "tracking"])
    if chrony.ok:
        offset_ms, leap = parse_chrony(chrony.stdout)
        if leap is not None and leap.lower() != "normal":
            return Outcome(
                "Время",
                Level.FAIL,
                f"chrony: {leap}",
                "Проверьте доступность NTP-сервера: chronyc sources",
            )
        if offset_ms is None:
            return Outcome("Время", Level.WARN, "chrony: не удалось прочитать смещение")
        if offset_ms > max_offset_ms:
            return Outcome(
                "Время",
                Level.FAIL,
                f"смещение {offset_ms:.1f} мс > {max_offset_ms:g} мс",
                "Синхронизируйте время: события разных камер сопоставляются по timestamp",
            )
        return Outcome("Время", Level.OK, f"chrony: смещение {offset_ms:.1f} мс")
    timedatectl = runner(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if timedatectl.ok:
        if timedatectl.stdout.strip() == "yes":
            return Outcome(
                "Время", Level.OK, "systemd-timesyncd: синхронизировано (смещение не измеряется)"
            )
        return Outcome(
            "Время", Level.FAIL, "время не синхронизировано с NTP", "Установите и настройте chrony"
        )
    return Outcome(
        "Время",
        Level.WARN,
        "не найдены chrony и timedatectl — синхронизацию проверить нельзя",
        "Установите chrony (SPEC.md §3.1)",
    )


# ---------------------------------------------------------------- секреты и конфиг


def check_secrets(directory: Path) -> Outcome:
    if not directory.is_dir():
        return Outcome(
            "Секреты",
            Level.FAIL,
            f"{directory} не найден",
            "Запустите ./install.sh — он сгенерирует секреты",
        )
    missing = [name for name in REQUIRED_SECRETS if not (directory / name).is_file()]
    if missing:
        return Outcome(
            "Секреты",
            Level.FAIL,
            f"нет файлов: {', '.join(missing)}",
            "Запустите ./install.sh повторно",
        )
    mode = stat.S_IMODE(directory.stat().st_mode)
    if mode & 0o077:
        return Outcome(
            "Секреты",
            Level.FAIL,
            f"права каталога {directory} = {mode:o}, доступ есть у группы/других",
            f"chmod 700 {directory}",
        )
    return Outcome("Секреты", Level.OK, f"{directory} (права 700)")


def check_config(config_dir: Path | None) -> tuple[Outcome, QoimaConfig | None]:
    try:
        cfg = load_config(config_dir)
    except ConfigError as exc:
        return Outcome(
            "Конфигурация",
            Level.FAIL,
            "; ".join(exc.errors),
            "Исправьте configs/ и запустите `qoima config validate`",
        ), None
    return Outcome("Конфигурация", Level.OK, f"объект «{cfg.defaults.site.name}»"), cfg


# ---------------------------------------------------------------- контейнеры


def check_containers(runner: Runner, plan: ComposePlan) -> list[Outcome]:
    ps = runner(plan.command("ps", "--all", "--format", "json"))
    if not ps.ok:
        return [Outcome("Контейнеры", Level.SKIP, "не удалось получить список контейнеров")]
    services = parse_ps(ps.stdout)
    if not services:
        return [Outcome("Контейнеры", Level.SKIP, "система не запущена", "Запустите: qoima up")]
    outcomes = []
    for svc in sorted(services, key=lambda s: str(s.get("Service"))):
        name = str(svc.get("Service"))
        state = str(svc.get("State", ""))
        health = str(svc.get("Health", ""))
        if state != "running":
            outcomes.append(
                Outcome(
                    f"сервис {name}",
                    Level.FAIL,
                    state or "не запущен",
                    f"docker compose logs {name}",
                )
            )
        elif health in ("", "healthy"):
            outcomes.append(Outcome(f"сервис {name}", Level.OK, health or "running"))
        elif health == "starting":
            outcomes.append(Outcome(f"сервис {name}", Level.WARN, "запускается"))
        else:
            outcomes.append(
                Outcome(
                    f"сервис {name}",
                    Level.FAIL,
                    health,
                    f"qoima status; docker compose logs {name}",
                )
            )
    return outcomes


# ---------------------------------------------------------------- камеры


def check_cameras(runner: Runner, plan: ComposePlan) -> list[Outcome]:
    """Состояние каждой камеры по последнему снимку Camera Health (из контейнера core-api)."""
    res = runner(plan.command("exec", "-T", "core-api", "python", "-m", "core_api.diagnostics"))
    if not res.ok:
        return [Outcome("Камеры", Level.SKIP, "не удалось получить состояние камер из core-api")]
    try:
        rows = json.loads(res.stdout)
    except ValueError:
        return [Outcome("Камеры", Level.SKIP, "core-api вернул некорректный ответ")]
    if not rows:
        return [
            Outcome(
                "Камеры", Level.WARN, "камеры не подключены", "Мастер настройки → шаг 3 «Камеры»"
            )
        ]
    outcomes = []
    for r in rows:
        status = r.get("status")
        level = {"ok": Level.OK, "degraded": Level.WARN, "down": Level.FAIL}.get(status, Level.WARN)
        outcomes.append(
            Outcome(
                f"камера {r.get('id')}",
                level,
                f"{r.get('name')}: {r.get('detail')}",
                r.get("recommendation", ""),
            )
        )
    return outcomes


# ---------------------------------------------------------------- отчёт для UI


def write_report(root: Path, outcomes: list[Outcome], gpus: list[Gpu], version: str) -> Path:
    """Отчёт doctor в каталог данных: core-api показывает его в мастере и в «Состоянии системы»."""
    report = DoctorReport(
        generated_at=datetime.now(UTC),
        hostname=socket.gethostname() or platform.node() or "server",
        version=version,
        outcomes=[
            DoctorOutcome(
                name=o.name,
                level=DoctorLevel(o.level.value),
                detail=o.detail[:2000],
                recommendation=o.recommendation[:1000],
            )
            for o in outcomes
        ],
        gpus=[
            DoctorGpu(index=g.index, name=g.name, driver=g.driver, vram_mb=g.vram_mb) for g in gpus
        ],
    )
    target = root / "doctor" / "report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".report-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(report.model_dump_json(indent=2))
    Path(tmp).chmod(0o644)
    Path(tmp).replace(target)
    return target
