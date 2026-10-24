"""Отпечаток железа для BenchResult и доступ к памяти GPU (NVML)."""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import av

from shared.schemas import GPUInfo, HardwareFingerprint
from vision_pipeline import __version__
from vision_pipeline.bench.decode import nvdec_available


def _nvidia_smi(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["nvidia-smi", *args], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def gpus() -> list[GPUInfo]:
    rows = _nvidia_smi(
        "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader,nounits"
    )
    if not rows:
        return []
    header = _nvidia_smi() or ""
    m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", header)
    cuda = m.group(1) if m else "unknown"
    nvdec = nvdec_available()
    out = []
    for line in rows.strip().splitlines():
        idx, name, mem, driver = (x.strip() for x in line.split(",", 3))
        out.append(
            GPUInfo(
                index=int(idx),
                name=name,
                vram_total_mb=int(float(mem)),
                nvdec_available=nvdec,
                driver_version=driver,
                cuda_version=cuda,
            )
        )
    return out


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def ram_total_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**20)
    except (ValueError, OSError, AttributeError):
        return 1


def fingerprint() -> HardwareFingerprint:
    try:
        import onnxruntime as ort

        ort_version: str | None = ort.__version__
        providers = list(ort.get_available_providers())
    except ImportError:
        ort_version, providers = None, []
    avc = av.library_versions["libavcodec"]
    return HardwareFingerprint(
        hostname=socket.gethostname()[:255] or "unknown",
        cpu_model=cpu_model()[:200],
        cpu_cores=os.cpu_count() or 1,
        ram_total_mb=ram_total_mb(),
        gpus=gpus(),
        onnxruntime=ort_version,
        providers=providers,
        libavcodec=".".join(str(x) for x in avc),
        pipeline_version=__version__,
    )


def vram_meter(index: int) -> Callable[[], float | None] | None:
    """Функция «занято VRAM на GPU index, МБ» через NVML; None — NVML недоступен."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(index)
    except Exception:  # нет драйвера NVIDIA / NVML (CPU-узел, WSL без NVML)
        return None

    def used() -> float | None:
        try:
            return float(pynvml.nvmlDeviceGetMemoryInfo(handle).used) / 2**20
        except Exception:
            return None

    return used
