"""Планировщик мощности на синтетических замерах: воспроизводимость, порядок понижения FPS,
минимумы сценариев, перегрузка декодирования, серая зона (t_low не поднимается), VRAM и VLM,
узел без GPU."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core_api.capacity.planner import (
    CameraDemand,
    GrayStats,
    PlannerInputs,
    camera_fps,
    plan_capacity,
)
from shared.config import QoimaConfig
from shared.schemas import BenchResult, FitVerdict, ScenarioKind

T = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
FALL, RED, SMOKE = ScenarioKind.FALL, ScenarioKind.RED_ZONE, ScenarioKind.SMOKE_FIRE


def gpu(vram_mb: int = 46068, index: int = 0) -> dict[str, Any]:
    return {
        "index": index,
        "name": "NVIDIA L40S",
        "vram_total_mb": vram_mb,
        "nvdec_available": True,
        "driver_version": "580.95",
        "cuda_version": "13.0",
    }


def bench(
    *,
    gpus: list[dict[str, Any]] | None = None,
    nvdec_fps: float = 2400.0,
    cpu_fps: float = 600.0,
    det_thr: float = 400.0,
    pose_thr: float = 1500.0,
    vlm_rps: float | None = None,
    models: bool = True,
) -> BenchResult:
    gpus = [gpu()] if gpus is None else gpus
    decode = [
        {
            "codec": "h264",
            "width": 640,
            "height": 360,
            "source_fps": 15,
            "backend": "cpu",
            "parallel_streams": 8,
            "fps_total": cpu_fps,
            "cv": 0.02,
            "source": "synthetic",
        }
    ]
    if gpus:
        decode.append(
            {
                "codec": "h264",
                "width": 640,
                "height": 360,
                "source_fps": 15,
                "backend": "nvdec",
                "gpu_index": 0,
                "parallel_streams": 32,
                "fps_total": nvdec_fps,
                "cv": 0.03,
                "vram_per_stream_mb": 60,
                "source": "synthetic",
            }
        )
    runtime = "tensorrt" if gpus else "onnxruntime"

    def m(name: str, batch: int, thr: float, p95: float) -> dict[str, Any]:
        return {
            "model": name,
            "version": f"{name}-1.0.0",
            "runtime": runtime,
            "providers": ["CPUExecutionProvider"],
            "batch": batch,
            "latency_p50_ms": p95 * 0.8,
            "latency_p95_ms": p95,
            "throughput": thr,
            "cv": 0.03,
            "vram_mb": 900 if gpus else None,
        }

    model_rows = (
        [
            m("rfdetr-person", 1, det_thr / 4, 5),
            m("rfdetr-person", 8, det_thr, 20),
            m("rfdetr-person", 16, det_thr * 1.5, 400),  # быстрее, но вне бюджета задержки
            m("rtmpose-body", 1, pose_thr, 3),
        ]
        if models
        else []
    )
    vlm = (
        [
            {
                "server": "vllm",
                "model": "Qwen3-VL-8B-Instruct-FP8",
                "concurrency": 4,
                "requests": 16,
                "rps": vlm_rps,
                "latency_p50_s": 1.2,
                "latency_p95_s": 2.0,
                "valid_json_ratio": 1.0,
            }
        ]
        if vlm_rps is not None
        else []
    )
    return BenchResult.model_validate(
        {
            "id": "bench-20261021-080000",
            "started_at": T,
            "finished_at": T + timedelta(minutes=5),
            "hardware": {
                "hostname": "srv",
                "cpu_model": "EPYC",
                "cpu_cores": 32,
                "ram_total_mb": 128000,
                "gpus": gpus,
                "onnxruntime": "1.31.0",
                "providers": ["CPUExecutionProvider"],
                "libavcodec": "62.11.100",
                "pipeline_version": "0.4.0",
            },
            "repeats": 3,
            "decode": decode,
            "models": model_rows,
            "vlm": vlm,
        }
    )


def cams(
    n: int, scenarios: tuple[ScenarioKind, ...], prefix: str = "cam", fps: float = 15
) -> tuple[CameraDemand, ...]:
    return tuple(
        CameraDemand(f"{prefix}-{i:02d}", scenarios, "h264", 640, 360, fps) for i in range(n)
    )


def plan(
    config: QoimaConfig,
    b: BenchResult,
    c: tuple[CameraDemand, ...],
    gray: dict[ScenarioKind, GrayStats] | None = None,
):
    return plan_capacity(PlannerInputs(b, c, gray or {}), config, T)


def test_plan_is_reproducible(config: QoimaConfig) -> None:
    b, c = bench(), cams(4, (FALL,))
    p1 = plan_capacity(PlannerInputs(b, c), config, T)
    p2 = plan_capacity(PlannerInputs(b, c), config, T + timedelta(days=1))
    assert p1.id == p2.id and p1.inputs_digest == p2.inputs_digest
    assert p1.model_dump(exclude={"created_at"}) == p2.model_dump(exclude={"created_at"})
    assert plan(config, b, cams(5, (FALL,))).inputs_digest != p1.inputs_digest


def test_small_site_fits_but_no_vlm_yet(config: QoimaConfig) -> None:
    p = plan(config, bench(), cams(4, (FALL,)) + cams(2, (RED,), "door"))
    assert p.verdict is FitVerdict.FITS_WITH_LIMITATIONS  # VLM ещё не измерен (Фаза 4)
    assert any("VLM не измерен" in w for w in p.warnings)
    fps = camera_fps(p)
    assert fps["cam-00"] == config.scenarios[FALL].target_fps
    assert fps["door-00"] == config.scenarios[RED].target_fps
    assert not any(a.degraded for a in p.allocations)
    assert p.vlm is None and p.llm_gpu_memory_utilization is None
    use = {u.resource: u for u in p.utilization}
    assert use["inference:gpu:0"].ratio <= 1 and use["decode:nvdec:0"].measured


def test_with_measured_vlm_and_vram_plan_fits(config: QoimaConfig) -> None:
    p = plan(config, bench(vlm_rps=4.0), cams(4, (FALL,)))
    assert p.verdict is FitVerdict.FITS
    assert p.vlm is not None and p.vlm.load_ratio <= config.defaults.routing.vlm_max_load_ratio
    v = p.vram[0]
    assert v.llm_mb > config.defaults.capacity.llm_min_mb
    assert p.llm_gpu_memory_utilization == pytest.approx(int(v.llm_mb / v.total_mb * 100) / 100)


def test_overload_degrades_least_important_first_not_below_minimum(config: QoimaConfig) -> None:
    # Детектор: 40 изобр/с; спрос 6 камер падения и 6 камер зоны на целевом FPS — 45 изобр/с.
    b = bench(det_thr=40.0, pose_thr=1e6)
    p = plan(config, b, cams(6, (FALL,)) + cams(6, (RED,), "door"))
    fps = camera_fps(p)
    red_min = type(config.scenarios[RED]).fps_range[0]
    fall_min = type(config.scenarios[FALL]).fps_range[0]
    red_target = config.scenarios[RED].target_fps
    fall_target = config.scenarios[FALL].target_fps
    assert p.verdict is FitVerdict.FITS_WITH_LIMITATIONS
    assert any("FPS понижен" in w for w in p.warnings)
    # Пока камеры падения не тронуты, зона опускается вплоть до своего минимума.
    if any(fps[f"cam-{i:02d}"] < fall_target for i in range(6)):
        assert all(fps[f"door-{i:02d}"] == red_min for i in range(6))
    assert all(fps[f"door-{i:02d}"] < red_target for i in range(6))
    assert all(f >= fall_min for k, f in fps.items() if k.startswith("cam"))
    assert all(f >= red_min for k, f in fps.items() if k.startswith("door"))
    use = {u.resource: u for u in p.utilization}
    assert use["inference:gpu:0"].used <= config.defaults.capacity.utilization_target + 1e-9


def test_does_not_fit_even_at_minimums(config: QoimaConfig) -> None:
    p = plan(config, bench(det_thr=10.0), cams(20, (FALL,)))
    assert p.verdict is FitVerdict.DOES_NOT_FIT
    assert all(f == type(config.scenarios[FALL]).fps_range[0] for f in camera_fps(p).values())
    assert any("не помещается" in w for w in p.warnings)


def test_decode_overload_is_not_fixed_by_lower_fps(config: QoimaConfig) -> None:
    # NVDEC: 300 к/с на 640×360, а 30 камер × 15 к/с = 450 к/с — понижение FPS анализа не поможет.
    p = plan(config, bench(nvdec_fps=300.0), cams(30, (FALL,)))
    assert p.verdict is FitVerdict.DOES_NOT_FIT
    assert any("декодирование" in w for w in p.warnings)


def test_gray_zone_narrows_only_by_lowering_t_high(config: QoimaConfig) -> None:
    # VLM 0.01 запр/с при оценке 4 запр/ч на камеру × 20 камер → нужно сужать.
    p = plan(config, bench(vlm_rps=0.01, det_thr=1e5), cams(20, (FALL,)))
    base = config.scenarios[FALL].thresholds
    zone = p.gray_zone[FALL]
    assert zone.t_low == base.t_low  # события ниже t_low не создаются — его поднимать нельзя
    assert zone.t_high < base.t_high
    assert zone.t_high - zone.t_low >= config.defaults.capacity.gray_zone_min_width - 1e-9
    assert (
        p.vlm is not None and p.vlm.load_ratio <= config.defaults.routing.vlm_max_load_ratio + 1e-9
    )
    assert any("серая зона сужена" in w for w in p.warnings)


def test_gray_zone_uses_real_score_distribution(config: QoimaConfig) -> None:
    base = config.scenarios[FALL].thresholds
    # Почти все реальные оценки серой зоны — у нижней границы.
    scores = tuple(base.t_low + 0.01 for _ in range(90)) + tuple(
        base.t_high - 0.01 for _ in range(10)
    )
    stats = {FALL: GrayStats(events=100, camera_hours=20 * 24 * 7, scores=scores)}
    p = plan(config, bench(vlm_rps=0.02, det_thr=1e5), cams(20, (FALL,)), stats)
    assert p.gray_zone[FALL] == base  # фактический поток мал — сужать не нужно


def test_small_gpu_runs_without_vlm(config: QoimaConfig) -> None:
    p = plan(config, bench(gpus=[gpu(16384)], vlm_rps=4.0), cams(2, (FALL,)))
    assert p.vlm is None and p.verdict is FitVerdict.FITS_WITH_LIMITATIONS
    assert any("без локального VLM" in w for w in p.warnings)


def test_two_gpus_second_for_worker(config: QoimaConfig) -> None:
    p = plan(config, bench(gpus=[gpu(), gpu(index=1)]), cams(2, (FALL,)))
    assert [v.gpu_index for v in p.vram] == [0, 1]
    assert p.vram[1].worker_mb > 0 and p.vram[1].pipeline_mb == 0
    assert all(a.gpu_index == 0 for a in p.allocations)


def test_cpu_node_plan(config: QoimaConfig) -> None:
    p = plan(config, bench(gpus=[]), cams(2, (FALL,)))
    assert p.gpus == [] and p.vram == [] and all(a.gpu_index is None for a in p.allocations)
    assert p.verdict is not FitVerdict.FITS
    assert [u.resource for u in p.utilization] == ["cpu"]


def test_no_detector_no_scenarios(config: QoimaConfig) -> None:
    p = plan(config, bench(models=False), cams(3, (FALL, SMOKE)))
    assert p.allocations == [] and p.verdict is FitVerdict.FITS_WITH_LIMITATIONS
    assert any("детектор людей" in w for w in p.warnings)
    assert any("дым/огонь" in w for w in p.warnings)


def test_unmeasured_codec_is_reported(config: QoimaConfig) -> None:
    hevc = (CameraDemand("cam-hevc", (FALL,), "hevc", 640, 360, 15),)
    p = plan(config, bench(vlm_rps=4.0), cams(1, (FALL,)) + hevc)
    assert p.verdict is FitVerdict.FITS_WITH_LIMITATIONS
    assert any("не измерено" in w and "cam-hevc" in w for w in p.warnings)


def test_resolution_scaling_uses_pixel_rate(config: QoimaConfig) -> None:
    # 1280×720 — в 4 раза больше пикселей: ёмкость в кадрах вчетверо меньше.
    small = plan(config, bench(), cams(4, (FALL,)))
    big = plan(
        config,
        bench(),
        tuple(CameraDemand(f"c{i}", (FALL,), "h264", 1280, 720, 15) for i in range(4)),
    )
    u_small = {u.resource: u.used for u in small.utilization}["decode:nvdec:0"]
    u_big = {u.resource: u.used for u in big.utilization}["decode:nvdec:0"]
    assert u_big == pytest.approx(u_small * 4, rel=1e-3)
