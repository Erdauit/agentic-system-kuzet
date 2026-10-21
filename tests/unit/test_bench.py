"""Бенчмарк узла без GPU: декодирование настоящего H.264, модели на ONNX-фикстурах, VLM на
поддельном OpenAI-совместимом сервере, сборка и сохранение BenchResult."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from shared.config import QoimaConfig
from shared.schemas import BenchResult, DecodeBackend, VLMVerdict
from tests.unit.test_inference import const_model, write_manifest
from vision_pipeline.bench import __main__ as bench_main
from vision_pipeline.bench.decode import decode_fps, measure_decode, synthetic_sample
from vision_pipeline.bench.models import measure_model
from vision_pipeline.bench.vlm import arbiter_request, measure_vlm


def quick(config: QoimaConfig) -> object:
    return config.defaults.bench.model_copy(
        update={
            "repeats": 2,
            "decode_seconds": 0.3,
            "decode_sample_seconds": 1.0,
            "max_parallel_decoders": 2,
            "model_iterations": 3,
            "model_warmup": 1,
            "vlm_concurrency": [1, 2],
            "vlm_requests_per_level": 4,
        }
    )


def test_decode_synthetic_h264_on_cpu(config: QoimaConfig) -> None:
    prof = config.defaults.bench.synthetic_profiles[0]
    sample = synthetic_sample(prof, 1.0)
    assert sample.codec == "h264" and (sample.width, sample.height) == (prof.width, prof.height)
    assert len(sample.packets) == int(prof.fps) and sample.extradata
    fps, errors = decode_fps(sample, DecodeBackend.CPU, None, 1, 0.3, (160, 90))
    assert fps > 0 and not errors
    m, warnings = measure_decode(
        sample, DecodeBackend.CPU, None, quick(config), config.defaults.pipeline.motion
    )  # type: ignore[arg-type]
    assert m is not None and not warnings
    assert m.backend is DecodeBackend.CPU and m.gpu_index is None and m.fps_total > 0
    assert 1 <= m.parallel_streams <= 2 and m.cv >= 0


def test_nvdec_unavailable_is_reported_not_faked(config: QoimaConfig) -> None:
    sample = synthetic_sample(config.defaults.bench.synthetic_profiles[0], 1.0)
    m, warnings = measure_decode(
        sample, DecodeBackend.NVDEC, 0, quick(config), config.defaults.pipeline.motion
    )  # type: ignore[arg-type]
    if m is None:  # на машине без CUDA
        assert warnings and "nvdec" in warnings[0]


def test_model_bench_on_fixture(tmp_path: Path, config: QoimaConfig) -> None:
    folder = tmp_path / "rfdetr-person"
    folder.mkdir()
    const_model(
        folder / "model.onnx", (3, 64, 64), {"dets": np.zeros((5, 4)), "labels": np.zeros((5, 3))}
    )
    rt = {
        "format": "onnx",
        "input_name": "input",
        "input_width": 64,
        "input_height": 64,
        "color": "rgb",
        "mean": [0, 0, 0],
        "std": [1, 1, 1],
        "scale": 255.0,
        "outputs": ["dets", "labels"],
        "dynamic_batch": True,
        "decoder": "rfdetr",
        "class_ids": {"person": 1},
    }
    write_manifest(folder, "rfdetr-person", rt)
    inf = config.defaults.pipeline.inference.model_copy(update={"max_batch": 4, "threads": 1})
    ms, warnings = measure_model(tmp_path, "rfdetr-person", inf, quick(config), None, "cpu")  # type: ignore[arg-type]
    assert [m.batch for m in ms] == [1, 2, 4] and not warnings
    assert all(m.throughput > 0 and m.latency_p95_ms >= m.latency_p50_ms for m in ms)
    assert ms[0].providers == ["CPUExecutionProvider"]
    missing, warn = measure_model(tmp_path, "rtmpose-body", inf, quick(config), None, "cpu")  # type: ignore[arg-type]
    assert missing == [] and "не измерена" in warn[0]


GOOD = VLMVerdict(
    scene_description="Человек лежит у пресса",
    hazard_assessment="CRITICAL",
    suggested_priority_change="RAISE",
    reasoning="Неподвижен",
    consistency_ok=True,
).model_dump_json()


async def test_vlm_bench_counts_only_valid_json(config: QoimaConfig) -> None:
    seen: list[dict[str, object]] = []
    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen3-VL-8B", "owned_by": "vllm"}]})
        body = json.loads(request.content)
        seen.append(body)
        n["i"] += 1
        content = GOOD if n["i"] % 4 else "не JSON"  # каждый 4-й ответ невалиден
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    ms, warnings = await measure_vlm(
        "http://llm:8000",
        None,
        [b"\xff\xd8a"] * 4,
        quick(config),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    assert not warnings and [m.concurrency for m in ms] == [1, 2]
    assert all(m.server == "vllm" and m.model == "Qwen3-VL-8B" for m in ms)
    assert all(m.valid_json_ratio == pytest.approx(0.75) for m in ms)
    req = seen[0]
    assert req["response_format"]["json_schema"]["name"] == "vlm_verdict"  # type: ignore[index]
    images = [c for c in req["messages"][1]["content"] if c["type"] == "image_url"]  # type: ignore[index]
    assert len(images) == 4


async def test_vlm_unreachable_is_a_warning(config: QoimaConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сервера")

    ms, warnings = await measure_vlm(
        "http://llm:8000", None, [], quick(config), transport=httpx.MockTransport(handler)
    )  # type: ignore[arg-type]
    assert ms == [] and "недоступен" in warnings[0]


def test_arbiter_request_shape() -> None:
    req = arbiter_request("m", [b"x", b"y"])
    assert req["temperature"] == 0 and req["messages"][0]["role"] == "system"


def test_full_run_writes_valid_result(
    tmp_path: Path, config: QoimaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"bench": quick(config)})}
    )
    args = argparse.Namespace(
        out=tmp_path,
        vlm_url=None,
        vlm_model=None,
        models_dir=str(tmp_path / "none"),
        no_cameras=True,
        quick=False,
    )
    result = bench_main.run(args, cfg)
    path = bench_main.save(result, tmp_path)
    loaded = BenchResult.model_validate_json(path.read_text())
    assert loaded.id == result.id and path.name == f"{result.id}.json"
    assert {(m.codec, m.width) for m in loaded.decode} >= {("h264", 640)}
    assert loaded.models == [] and any("не измерена" in w for w in loaded.warnings)
    assert any("VLM не измерялся" in w for w in loaded.warnings)
