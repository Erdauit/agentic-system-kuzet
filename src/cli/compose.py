"""Сборка команды docker compose под конфигурацию сервера (1 GPU / 2 GPU / без GPU)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cli import __version__
from cli.runner import Runner

PROJECT = "qoima"
GpuProfile = Literal["none", "gpu1", "gpu2"]


class ComposeError(RuntimeError):
    pass


def gpu_profile_for(gpu_count: int, *, allow_no_gpu: bool) -> GpuProfile:
    if gpu_count <= 0:
        if allow_no_gpu:
            return "none"
        raise ComposeError(
            "GPU NVIDIA не обнаружен. Система рассчитана на сервер с 1–2 GPU (SPEC.md §1). "
            "Для разработки без GPU используйте `qoima up --no-gpu`."
        )
    # SPEC.md §4.1: 1 GPU — всё на одной карте; 2 GPU — GPU1 отдан worker-gpu.
    return "gpu1" if gpu_count == 1 else "gpu2"


@dataclass(frozen=True)
class ComposePlan:
    home: Path
    gpu_profile: GpuProfile
    engineering: bool
    env: dict[str, str] = field(default_factory=dict)
    sim: bool = False

    @property
    def files(self) -> list[Path]:
        compose_dir = self.home / "compose"
        files = [compose_dir / "compose.yml"]
        if self.gpu_profile != "none":
            files.append(compose_dir / f"compose.{self.gpu_profile}.yml")
        if self.engineering:
            files.append(compose_dir / "compose.engineering.yml")
        if self.sim:
            files.append(compose_dir / "compose.sim.yml")
        return files

    def with_sim(self) -> ComposePlan:
        return ComposePlan(self.home, self.gpu_profile, self.engineering, self.env, sim=True)

    def command(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "--project-name", PROJECT]
        for f in self.files:
            cmd += ["--file", str(f)]
        return [*cmd, *args]

    def process_env(self) -> dict[str, str]:
        return {**os.environ, **self.env}


def build_plan(
    home: Path,
    *,
    gpu_count: int,
    allow_no_gpu: bool,
    engineering: bool,
    data_root: Path,
    secrets_dir: Path,
    https_port: int,
    host_subnets: list[str] | None = None,
    host_ips: list[str] | None = None,
) -> ComposePlan:
    return ComposePlan(
        home=home,
        gpu_profile=gpu_profile_for(gpu_count, allow_no_gpu=allow_no_gpu),
        engineering=engineering,
        env={
            "QOIMA_VERSION": os.environ.get("QOIMA_VERSION", __version__),
            "QOIMA_DATA_ROOT": str(data_root),
            "QOIMA_SECRETS_DIR": str(secrets_dir),
            "QOIMA_HTTPS_PORT": str(https_port),
            "QOIMA_HOST_SUBNETS": ",".join(host_subnets or []),
            "QOIMA_HOST_IPS": ",".join(host_ips or []),
        },
    )


def count_gpus(runner: Runner) -> int:
    result = runner(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"])
    if not result.ok:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def parse_ps(output: str) -> list[dict[str, Any]]:
    """`docker compose ps --format json`: новые версии пишут по объекту на строку, старые — массив."""
    text = output.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [d for d in data if isinstance(d, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]
