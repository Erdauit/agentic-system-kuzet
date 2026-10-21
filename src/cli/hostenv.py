"""Особенности хоста: Linux на сервере или WSL2 на Windows (стенд для проверки).

WSL2 — не целевая платформа объекта (там — Ubuntu LTS), но систему можно поднять и проверить на
машине с Windows и GPU NVIDIA. Отличия, которые здесь учитываются:
- nvidia-smi лежит в /usr/lib/wsl/lib, а sudo и systemd этот каталог в PATH не добавляют;
- Docker Desktop даёт GPU контейнерам без runtime «nvidia» в `docker info` — проверяем делом;
- виртуальный диск WSL (ext4.vhdx) растёт на диске Windows: свободное место внутри WSL ничего
  не говорит о месте на C:;
- сеть WSL по умолчанию — NAT: автопоиск камер видит только внутреннюю сеть WSL.
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

from cli.runner import Runner

WSL_NVIDIA_SMI = Path("/usr/lib/wsl/lib/nvidia-smi")
# Образ для проверки GPU в контейнере: уже нужен стеку (postgres) и содержит glibc для nvidia-smi.
GPU_PROBE_IMAGE = "postgres:16.15-bookworm"


WSL_INTEROP = Path("/proc/sys/fs/binfmt_misc/WSLInterop")


def is_wsl(release: str | None = None, interop: Path = WSL_INTEROP) -> bool:
    """Linux под Windows (WSL2): ядро Microsoft или интеграция WSL."""
    rel = platform.release() if release is None else release
    return "microsoft" in rel.lower() or interop.exists()


def nvidia_smi() -> str:
    """Путь к nvidia-smi: из PATH, иначе каталог драйвера WSL; без драйвера — просто имя."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if WSL_NVIDIA_SMI.exists():
        return str(WSL_NVIDIA_SMI)
    return "nvidia-smi"


def wsl_networking_mode(runner: Runner) -> str | None:
    """mirrored | nat | None (неизвестно: старый WSL без wslinfo)."""
    res = runner(["wslinfo", "--networking-mode"])
    if not res.ok:
        return None
    mode = res.stdout.strip().lower()
    return mode or None


def windows_drive(letter: str = "c") -> Path | None:
    path = Path(f"/mnt/{letter}")
    return path if path.is_dir() else None


def docker_gpu_works(runner: Runner, image: str = GPU_PROBE_IMAGE) -> tuple[bool, str]:
    """Проверка делом: контейнер с --gpus all видит GPU (nvidia-container-toolkit или Docker Desktop)."""
    res = runner(["docker", "run", "--rm", "--gpus", "all", image, "nvidia-smi", "-L"])
    text = (res.stdout or res.stderr).strip()
    return res.ok and "GPU" in res.stdout, text.splitlines()[0] if text else ""


def under_windows_mount(path: Path) -> bool:
    """Каталог на диске Windows (/mnt/c/...) — медленно и без прав Linux (chmod не работает)."""
    parts = path.resolve().parts
    return len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1
