"""Планировщик мощности (SPEC.md §3.2 шаг 6, §4.1, §5.10; Фаза 3).

Чистая детерминированная функция: замеры `qoima bench` + камеры и сценарии + конфиг →
CapacityPlan. Те же входы дают тот же план (inputs_digest, id = uuid5 от него).

Модель нагрузки:
- декодирование: каждый кадр потока анализа декодируется и уменьшается до миниатюры детектора
  движения, поэтому нагрузка — родной FPS потока, а не FPS анализа. Понижением FPS анализа её не
  уменьшить: если декодирование не влезает — план «не подходит»;
- инференс: детектор людей на FPS анализа при движении (доля времени motion_duty_cycle) и на
  сторожевом FPS без движения; поза — на каждом человеке в кадре (persons_per_frame);
- ёмкость — замер bench (лучший батч с p95 ≤ inference_latency_budget_ms), используем не больше
  utilization_target от неё;
- не влезает — FPS понижается шагом fps_step сначала у камер с наименее важным сценарием
  (capacity.scenario_priority), но не ниже минимума сценария из SPEC.md §5.4;
- серая зона: ожидаемый поток к арбитру ≤ routing.vlm_max_load_ratio × ёмкость VLM. Сужается
  только снижением t_high (больше событий уходит сразу, без арбитра); t_low не поднимается
  никогда — события ниже него не создаются вовсе, это было бы подавлением тревог (§2.1).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from shared.config import QoimaConfig
from shared.config.models import ScenarioConfig
from shared.schemas import (
    BenchResult,
    CameraAllocation,
    CapacityPlan,
    DecodeBackend,
    DecodeMeasurement,
    FitVerdict,
    ModelMeasurement,
    ResourceUse,
    ScenarioKind,
    ThresholdPair,
    VlmCapacity,
    VlmMeasurement,
    VramBudget,
)

PLAN_NAMESPACE = uuid.UUID("5d0f8a0e-7c41-4b8e-9a1f-3b2c6d7e8f90")
# Сценарии, которые hot path умеет выполнять с детектором людей (vision_pipeline.hotpath.analyzer).
PERSON_SCENARIOS = frozenset({ScenarioKind.FALL, ScenarioKind.RED_ZONE})


@dataclass(frozen=True)
class CameraDemand:
    """Камера и её поток анализа (substream, а без него — mainstream) по зондированию."""

    camera_id: str
    scenarios: tuple[ScenarioKind, ...]
    codec: str
    width: int
    height: int
    native_fps: float


@dataclass(frozen=True)
class GrayStats:
    """Фактическая серая зона сценария за capacity.gray_stats_days (события с запросом арбитру)."""

    events: int
    camera_hours: float
    scores: tuple[float, ...] = field(default_factory=tuple)

    @property
    def rate_per_camera_hour(self) -> float:
        return self.events / self.camera_hours if self.camera_hours > 0 else 0.0


@dataclass(frozen=True)
class PlannerInputs:
    bench: BenchResult
    cameras: tuple[CameraDemand, ...]
    gray: dict[ScenarioKind, GrayStats] = field(default_factory=dict)


# ---------------------------------------------------------------- выбор замеров


def best_model(
    models: Iterable[ModelMeasurement], name: str, budget_ms: float
) -> ModelMeasurement | None:
    """Наибольшая пропускная способность среди батчей с p95 в пределах бюджета задержки."""
    own = [m for m in models if m.model == name]
    if not own:
        return None
    ok = [m for m in own if m.latency_p95_ms <= budget_ms]
    pool = ok or [min(own, key=lambda m: m.latency_p95_ms)]
    return max(pool, key=lambda m: (m.throughput, -m.batch))


def best_vlm(
    vlm: Iterable[VlmMeasurement], timeout_s: float, min_valid: float
) -> VlmMeasurement | None:
    ok = [
        v
        for v in vlm
        if v.valid_json_ratio >= min_valid and v.latency_p95_s <= timeout_s and v.rps > 0
    ]
    return max(ok, key=lambda v: (v.rps, -v.concurrency)) if ok else None


def decode_capacity(
    decode: Iterable[DecodeMeasurement], backend: DecodeBackend, cam: CameraDemand
) -> float | None:
    """Кадров/с для потока камеры: ближайший по размеру замер того же кодека, пересчёт по
    пиксельной скорости (время декодирования ~ число пикселей)."""
    same = [d for d in decode if d.backend is backend and d.codec == cam.codec]
    if not same:
        return None
    pixels = cam.width * cam.height
    nearest = min(same, key=lambda d: (abs(d.width * d.height - pixels), d.width))
    return nearest.pixel_rate / pixels


# ---------------------------------------------------------------- расчёт


def _digest(inputs: PlannerInputs, config: QoimaConfig) -> str:
    d = config.defaults
    payload = {
        "bench": inputs.bench.model_dump(mode="json"),
        "cameras": [
            c.__dict__ | {"scenarios": [s.value for s in c.scenarios]} for c in inputs.cameras
        ],
        "gray": {
            s.value: {"events": g.events, "camera_hours": g.camera_hours, "scores": list(g.scores)}
            for s, g in sorted(inputs.gray.items())
        },
        "config": {
            "capacity": d.capacity.model_dump(mode="json"),
            "routing": d.routing.model_dump(mode="json"),
            "vlm": d.vlm.model_dump(mode="json"),
            "motion": d.pipeline.motion.model_dump(mode="json"),
            "inference": d.pipeline.inference.model_dump(mode="json"),
            "backend": d.pipeline.backend,
            "scenarios": {
                k.value: v.model_dump(mode="json") for k, v in sorted(config.scenarios.items())
            },
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def plan_capacity(inputs: PlannerInputs, config: QoimaConfig, created_at: datetime) -> CapacityPlan:
    d = config.defaults
    cap = d.capacity
    bench = inputs.bench
    warnings: list[str] = []
    gpus = bench.hardware.gpus
    gpu0 = gpus[0].index if gpus else None
    target = cap.utilization_target

    if not gpus:
        warnings.append(
            "GPU нет: план для CPU — режим разработки; на объекте нужен GPU (SPEC.md §4.1)"
        )
    if bench.live_load and bench.live_load.cameras > 0:
        warnings.append(
            f"бенчмарк шёл при работающем пайплайне ({bench.live_load.cameras} камер): "
            "измерен запас сверх текущей нагрузки — план с запасом"
        )

    # --- декодирование
    backend = (
        DecodeBackend.NVDEC
        if gpus and any(m.backend is DecodeBackend.NVDEC for m in bench.decode)
        else DecodeBackend.CPU
    )
    decode_frac = 0.0
    unmeasured: list[str] = []
    for cam in inputs.cameras:
        capacity = decode_capacity(bench.decode, backend, cam)
        if capacity is None:
            unmeasured.append(f"{cam.camera_id} ({cam.codec} {cam.width}×{cam.height})")
            continue
        decode_frac += cam.native_fps / capacity
    if unmeasured:
        warnings.append(
            "декодирование не измерено для: "
            + ", ".join(unmeasured)
            + " — запустите qoima bench при подключённых камерах"
        )

    # --- модели и доступные сценарии
    inf = d.pipeline.inference
    det = best_model(bench.models, inf.person_model, cap.inference_latency_budget_ms)
    pose = (
        best_model(bench.models, inf.pose_model, cap.inference_latency_budget_ms)
        if inf.pose_model
        else None
    )
    for m, name in ((det, inf.person_model), (pose, inf.pose_model)):
        if m is not None and m.latency_p95_ms > cap.inference_latency_budget_ms:
            warnings.append(
                f"{name}: даже батч {m.batch} дольше бюджета задержки "
                f"({m.latency_p95_ms:.0f} > {cap.inference_latency_budget_ms:.0f} мс p95)"
            )
    available: set[ScenarioKind] = set(PERSON_SCENARIOS) if det is not None else set()
    if det is None:
        warnings.append(
            f"детектор людей {inf.person_model} не измерен (нет артефакта модели) — "
            "падение и опасная зона не работают"
        )
    disabled: dict[ScenarioKind, int] = {}
    for cam in inputs.cameras:
        for s in cam.scenarios:
            if s not in available:
                disabled[s] = disabled.get(s, 0) + 1
    if disabled.get(ScenarioKind.SMOKE_FIRE):
        warnings.append(
            f"дым/огонь включён на {disabled[ScenarioKind.SMOKE_FIRE]} камерах, но не развёрнут "
            "(нет весов модели)"
        )

    active = [
        (cam, tuple(s for s in cam.scenarios if s in available))
        for cam in sorted(inputs.cameras, key=lambda c: c.camera_id)
    ]
    active = [(cam, scen) for cam, scen in active if scen]
    idle = d.pipeline.motion.idle_fps
    duty = cap.motion_duty_cycle

    def scen_cfg(s: ScenarioKind) -> ScenarioConfig:
        return config.scenarios[s]

    targets = {cam.camera_id: max(scen_cfg(s).target_fps for s in scen) for cam, scen in active}
    minimums = {cam.camera_id: max(scen_cfg(s).fps_range[0] for s in scen) for cam, scen in active}
    rank = {
        cam.camera_id: min(cap.scenario_priority.index(s) for s in scen) for cam, scen in active
    }
    fps = dict(targets)

    def infer_frac() -> float:
        if det is None:
            return 0.0
        total = 0.0
        for f in fps.values():
            rate = f * duty + min(idle, f) * (1 - duty)
            total += rate / det.throughput
            if pose is not None:
                total += rate * cap.persons_per_frame / pose.throughput
        return total

    def fits() -> bool:
        if gpus:
            return infer_frac() <= target + 1e-9
        return (
            decode_frac + infer_frac() <= target + 1e-9
        )  # на CPU декодирование и инференс делят ядра

    decode_ok = decode_frac <= target + 1e-9
    while not fits():
        progressed = False
        for r in sorted(set(rank.values()), reverse=True):  # сначала наименее важные
            group = sorted(
                c for c in fps if rank[c] == r and fps[c] - cap.fps_step >= minimums[c] - 1e-9
            )
            if not group:
                continue
            for c in group:
                fps[c] = round(max(minimums[c], fps[c] - cap.fps_step), 3)
                if fits():
                    break
            progressed = True
            break
        if not progressed:
            break
    inference_ok = fits()
    if not decode_ok:
        warnings.append(
            f"декодирование: нужно {decode_frac:.0%} ёмкости {backend.value}, допустимо "
            f"{target:.0%} — уменьшите FPS или разрешение substream на камерах или их число"
        )
    if not inference_ok:
        over = infer_frac() + (0 if gpus else decode_frac)
        warnings.append(
            f"инференс не помещается даже на минимальном FPS сценариев ({over:.0%} при допустимых "
            f"{target:.0%}) — нужно больше GPU или меньше камер со сценариями"
        )
    degraded = sorted(c for c in fps if fps[c] < targets[c] - 1e-9)
    if degraded:
        warnings.append(
            "FPS понижен: " + ", ".join(f"{c} {targets[c]:g} → {fps[c]:g}" for c in degraded)
        )

    allocations = []
    for cam, scen in active:
        for s in sorted(scen, key=cap.scenario_priority.index):
            t = scen_cfg(s).target_fps
            fa = min(fps[cam.camera_id], t)
            allocations.append(
                CameraAllocation(
                    camera_id=cam.camera_id,
                    scenario=s,
                    fps_active=fa,
                    fps_idle=min(idle, fa),
                    target_fps=t,
                    gpu_index=gpu0,
                )
            )

    # --- VRAM и VLM
    vram: list[VramBudget] = []
    vlm_meas = best_vlm(bench.vlm, d.vlm.arbiter_timeout_s, cap.vlm_min_valid_json_ratio)
    llm_mb = 0
    llm_util: float | None = None
    vlm_ok = False
    if gpus:
        g0 = gpus[0]
        per_stream = max(
            (m.vram_per_stream_mb or 0 for m in bench.decode if m.backend is DecodeBackend.NVDEC),
            default=0,
        )
        model_mb = sum((m.vram_mb or 0) for m in (det, pose) if m is not None)
        pipeline_mb = cap.cuda_context_mb + model_mb + per_stream * len(active)
        free_for_llm = g0.vram_total_mb - pipeline_mb - cap.triton_reserve_mb - cap.vram_reserve_mb
        if g0.vram_total_mb < d.vlm.min_vram_gb * 1024:
            warnings.append(
                f"GPU{g0.index}: {g0.vram_total_mb // 1024} ГБ VRAM < {d.vlm.min_vram_gb:g} ГБ — "
                "режим без локального VLM: алерты без арбитража (SPEC.md §4.1)"
            )
        elif free_for_llm < cap.llm_min_mb:
            warnings.append(
                f"GPU{g0.index}: под VLM остаётся {max(free_for_llm, 0)} МБ < {cap.llm_min_mb} МБ — "
                "режим без локального VLM"
            )
        elif vlm_meas is None:
            warnings.append(
                f"VLM не измерен (сервис llm — Фаза 4): под него свободно {free_for_llm} МБ; "
                "пока события серой зоны уходят без арбитража (fail-open)"
            )
        else:
            llm_mb = free_for_llm
            llm_util = math.floor(llm_mb / g0.vram_total_mb * 100) / 100
            vlm_ok = True
        vram.append(
            VramBudget(
                gpu_index=g0.index,
                total_mb=g0.vram_total_mb,
                pipeline_mb=pipeline_mb,
                triton_mb=cap.triton_reserve_mb,
                llm_mb=llm_mb,
                worker_mb=0,
                reserve_mb=cap.vram_reserve_mb,
            )
        )
        for g in gpus[1:]:  # 2 GPU: вторая — обучение, авторазметка, эмбеддинги (SPEC.md §4.1)
            vram.append(
                VramBudget(
                    gpu_index=g.index,
                    total_mb=g.vram_total_mb,
                    pipeline_mb=0,
                    triton_mb=0,
                    llm_mb=0,
                    worker_mb=max(g.vram_total_mb - cap.vram_reserve_mb, 0),
                    reserve_mb=min(cap.vram_reserve_mb, g.vram_total_mb),
                )
            )
    elif vlm_meas is not None:
        warnings.append("VLM измерен, но на узле без GPU план его не использует")

    # --- серая зона
    gray_zone = {k: v.thresholds for k, v in sorted(config.scenarios.items())}
    cams_with: dict[ScenarioKind, int] = {}
    for _cam, scen in active:
        for s in scen:
            cams_with[s] = cams_with.get(s, 0) + 1

    def scen_rate(s: ScenarioKind, pair: ThresholdPair) -> float:
        """Запросов к арбитру в час на камеру при серой зоне pair."""
        base = config.scenarios[s].thresholds
        stats = inputs.gray.get(s)
        if stats is not None and stats.events >= cap.gray_stats_min_events and stats.scores:
            kept = sum(1 for x in stats.scores if pair.t_low <= x <= pair.t_high)
            return stats.rate_per_camera_hour * kept / len(stats.scores)
        rate = (
            stats.rate_per_camera_hour
            if stats is not None and stats.events >= cap.gray_stats_min_events
            else cap.gray_requests_per_camera_hour
        )
        # Без распределения оценок — равномерное в серой зоне (docs/ASSUMPTIONS.md).
        return rate * (pair.t_high - pair.t_low) / (base.t_high - base.t_low)

    def expected_rps(zone: dict[ScenarioKind, ThresholdPair]) -> float:
        return sum(n * scen_rate(s, zone[s]) for s, n in cams_with.items()) / 3600

    vlm_cap: VlmCapacity | None = None
    if vlm_ok and vlm_meas is not None:
        limit = d.routing.vlm_max_load_ratio * vlm_meas.rps
        expected = expected_rps(gray_zone)
        if expected > limit:
            k = limit / expected
            narrowed = {}
            for s, pair in gray_zone.items():
                if s not in cams_with:
                    narrowed[s] = pair
                    continue
                stats = inputs.gray.get(s)
                width = pair.t_high - pair.t_low
                if stats is not None and stats.events >= cap.gray_stats_min_events and stats.scores:
                    inside = sorted(x for x in stats.scores if pair.t_low <= x <= pair.t_high)
                    idx = max(int(k * len(inside)) - 1, 0)
                    new_high = inside[idx] if inside else pair.t_low + width * k
                else:
                    new_high = pair.t_low + width * k
                new_high = round(max(new_high, pair.t_low + cap.gray_zone_min_width), 3)
                narrowed[s] = ThresholdPair(t_low=pair.t_low, t_high=min(new_high, pair.t_high))
            gray_zone = narrowed
            expected = expected_rps(gray_zone)
            changed = [s.value for s in gray_zone if gray_zone[s] != config.scenarios[s].thresholds]
            warnings.append(
                "серая зона сужена (t_high снижен, t_low не меняется) для: " + ", ".join(changed)
            )
        if expected <= limit + 1e-12:
            vlm_cap = VlmCapacity(
                measured_rps=vlm_meas.rps,
                expected_rps=round(expected, 6),
                max_load_ratio=d.routing.vlm_max_load_ratio,
            )
        else:
            warnings.append(
                "VLM не справится даже с минимальной серой зоной — арбитраж планом отключён "
                "(события уходят без него, fail-open)"
            )
            llm_util = None
            vram = [v.model_copy(update={"llm_mb": 0}) for v in vram]

    # --- загрузка и вердикт
    utilization: list[ResourceUse] = []
    if gpus:
        utilization.append(
            ResourceUse(
                resource=f"decode:{backend.value}"
                + (f":{gpu0}" if backend is DecodeBackend.NVDEC else ""),
                used=round(decode_frac, 4),
                capacity=target,
                unit="доля",
                measured=bool(bench.decode),
            )
        )
        utilization.append(
            ResourceUse(
                resource=f"inference:gpu:{gpu0}",
                used=round(infer_frac(), 4),
                capacity=target,
                unit="доля",
                measured=det is not None,
            )
        )
        for v in vram:
            used = v.pipeline_mb + v.triton_mb + v.llm_mb + v.worker_mb + v.reserve_mb
            utilization.append(
                ResourceUse(
                    resource=f"vram:{v.gpu_index}",
                    used=used,
                    capacity=v.total_mb,
                    unit="МБ",
                    measured=True,
                )
            )
    else:
        utilization.append(
            ResourceUse(
                resource="cpu",
                used=round(decode_frac + infer_frac(), 4),
                capacity=target,
                unit="доля",
                measured=bool(bench.decode) and det is not None,
            )
        )
    if vlm_cap is not None:
        utilization.append(
            ResourceUse(
                resource="vlm",
                used=vlm_cap.expected_rps,
                capacity=round(vlm_cap.measured_rps * vlm_cap.max_load_ratio, 6),
                unit="запр/с",
                measured=True,
            )
        )

    if not decode_ok or not inference_ok:
        verdict = FitVerdict.DOES_NOT_FIT
    elif degraded or vlm_cap is None or not gpus or unmeasured or det is None:
        verdict = FitVerdict.FITS_WITH_LIMITATIONS
    else:
        verdict = FitVerdict.FITS

    digest = _digest(inputs, config)
    return CapacityPlan(
        id=uuid.uuid5(PLAN_NAMESPACE, digest),
        created_at=created_at,
        benchmark_id=bench.id,
        backend=d.pipeline.backend,
        gpus=gpus,
        allocations=allocations,
        vram=vram,
        vlm=vlm_cap,
        llm_gpu_memory_utilization=llm_util if vlm_cap is not None else None,
        gray_zone=gray_zone,
        verdict=verdict,
        warnings=warnings,
        utilization=utilization,
        inputs_digest=digest,
    )


def camera_fps(plan: CapacityPlan) -> dict[str, float]:
    """FPS анализа камеры по плану: максимум по её сценариям."""
    out: dict[str, float] = {}
    for a in plan.allocations:
        out[a.camera_id] = max(out.get(a.camera_id, 0.0), a.fps_active)
    return out
