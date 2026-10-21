"""Эталонные примеры для каждого контракта из shared.schemas.CONTRACTS.

Пример — валидный «типичный» объект. Контрактные тесты проверяют, что он проходит валидацию
и переживает JSON round-trip. Новому контракту нужен пример здесь и снапшот схемы
(`make schemas`).
"""

from __future__ import annotations

from typing import Any

from shared.schemas import AlertStream, ChannelKind, idempotency_key

T0 = "2026-10-19T08:00:00+05:00"
T1 = "2026-10-19T08:00:10+05:00"
T2 = "2026-10-19T08:01:00+05:00"
T3 = "2026-10-19T08:05:00+05:00"

INCIDENT_ID = "6f1c2a8e-3b4d-4c5e-9f60-718293a4b5c6"
EVENT_ID = "0a1b2c3d-4e5f-4061-8728-394a5b6c7d8e"
SYSTEM_ALERT_ID = "9e8d7c6b-5a49-4382-a716-05f4e3d2c1b0"

SQUARE = {
    "points": [
        {"x": 0.1, "y": 0.1},
        {"x": 0.6, "y": 0.1},
        {"x": 0.6, "y": 0.7},
        {"x": 0.1, "y": 0.7},
    ]
}

VERDICT = {
    "scene_description": "Человек лежит на полу у пресса, рядом никого",
    "hazard_assessment": "CRITICAL",
    "suggested_priority_change": "RAISE",
    "reasoning": "Поза неподвижна более 10 секунд, признаков плановых работ нет",
    "consistency_ok": True,
}

FRAME = {
    "blob_key": "frames/cam-07/2026/10/19/0001.jpg",
    "captured_at": T0,
    "bbox": {"x1": 0.2, "y1": 0.3, "x2": 0.4, "y2": 0.9},
}


def _delivery() -> dict[str, Any]:
    from uuid import UUID

    key = idempotency_key(AlertStream.SAFETY, UUID(INCIDENT_ID), ChannelKind.TELEGRAM, "-100123", 0)
    return {
        "idempotency_key": key,
        "stream": "safety",
        "channel": "telegram",
        "target": "-100123",
        "incident_id": INCIDENT_ID,
        "escalation_level": 0,
        "attempt": 1,
        "status": "sent",
        "created_at": T1,
        "sent_at": T1,
    }


SUB_PROBE = {
    "role": "sub",
    "url": "rtsp://10.0.5.17:554/Streaming/Channels/102",
    "codec": "h264",
    "width": 640,
    "height": 360,
    "fps": 15.0,
    "bitrate_kbps": 400,
    "gop_seconds": 2.0,
    "probed_at": T0,
}
MAIN_PROBE = {
    **SUB_PROBE,
    "role": "main",
    "url": "rtsp://10.0.5.17:554/Streaming/Channels/101",
    "width": 1920,
    "height": 1080,
    "bitrate_kbps": 4000,
}

DEVICE = {
    "host": "10.0.5.17",
    "methods": ["onvif_unicast", "rtsp_scan"],
    "onvif_xaddr": "http://10.0.5.17:80/onvif/device_service",
    "rtsp_port": 554,
    "manufacturer": "HIKVISION",
    "model": "DS-2CD2143G2-I",
    "firmware": "V5.7.3",
    "serial": "DS-2CD2143G2-I20230101AAWRL12345678",
    "name": "Линия 3",
    "auth": "ok",
    "channels": 1,
    "candidates": [
        {
            "url": "rtsp://10.0.5.17:554/Streaming/Channels/101",
            "source": "onvif_profile",
            "profile": "mainStream",
            "width": 1920,
            "height": 1080,
            "channel": 1,
        },
        {
            "url": "rtsp://10.0.5.17:554/Streaming/Channels/102",
            "source": "onvif_profile",
            "profile": "subStream",
            "width": 640,
            "height": 360,
            "channel": 1,
        },
    ],
    "errors": [],
}

EXAMPLES: dict[str, dict[str, Any]] = {
    "AlertDelivery": _delivery(),
    "ArbiterOutcome": {
        "incident_id": INCIDENT_ID,
        "mode": "assist",
        "verdict": VERDICT,
        "applied_priority_change": "RAISE",
        "latency_seconds": 1.8,
        "decided_at": T1,
    },
    "AuditRecord": {
        "id": "11111111-2222-4333-8444-555555555555",
        "at": T2,
        "actor": "shift_lead:ivanov",
        "action": "incident_transition",
        "target": f"incident:{INCIDENT_ID}",
        "details": {"from": "OPEN", "to": "ACKNOWLEDGED"},
    },
    "CameraConfig": {
        "id": "cam-07",
        "name": "Линия упаковки, вид сверху",
        "source": "onvif",
        "main_url": "rtsp://10.0.5.17:554/Streaming/Channels/101",
        "sub_url": "rtsp://10.0.5.17:554/Streaming/Channels/102",
        "credentials_secret": "cam-07",
        "scenarios": ["fall", "red_zone"],
        "location": "Цех 2, линия 3",
    },
    "CameraHealth": {
        "camera_id": "cam-07",
        "checked_at": T0,
        "status": "degraded",
        "stream_alive": True,
        "fps_in": 25.0,
        "fps_planned": 8.0,
        "fps_processed": 8.0,
        "reconnects_total": 2,
        "decode_errors_total": 0,
        "last_packet_at": T0,
        "brightness": 118.4,
        "frozen": False,
        "black": False,
        "overexposed": False,
        "sharpness_ratio": 0.35,
        "occluded_ratio": 0.0,
        "view_shift_score": 0.05,
        "issues": ["blur"],
    },
    "CapacityPlan": {
        "id": "22222222-3333-4444-8555-666666666666",
        "created_at": T0,
        "benchmark_id": "bench-2026-10-19-01",
        "backend": "lite",
        "gpus": [
            {
                "index": 0,
                "name": "NVIDIA L40S",
                "vram_total_mb": 46068,
                "nvdec_available": True,
                "driver_version": "580.95",
                "cuda_version": "13.0",
            }
        ],
        "allocations": [
            {
                "camera_id": "cam-07",
                "scenario": "fall",
                "fps_active": 8,
                "fps_idle": 1,
                "target_fps": 8,
                "gpu_index": 0,
            },
            {
                "camera_id": "cam-08",
                "scenario": "smoke_fire",
                "fps_active": 2,
                "fps_idle": 1,
                "target_fps": 3,
                "gpu_index": 0,
            },
        ],
        "vram": [
            {
                "gpu_index": 0,
                "total_mb": 46068,
                "pipeline_mb": 9000,
                "triton_mb": 4000,
                "llm_mb": 24000,
                "worker_mb": 0,
                "reserve_mb": 4000,
            }
        ],
        "vlm": {"measured_rps": 4.0, "expected_rps": 1.2, "max_load_ratio": 0.7},
        "llm_gpu_memory_utilization": 0.52,
        "gray_zone": {
            "fall": {"t_low": 0.4, "t_high": 0.8},
            "red_zone": {"t_low": 0.4, "t_high": 0.75},
            "smoke_fire": {"t_low": 0.35, "t_high": 0.7},
        },
        "verdict": "fits_with_limitations",
        "warnings": ["cam-08: FPS понижен с 3 до 2"],
        "utilization": [
            {
                "resource": "decode:nvdec:0",
                "used": 0.41,
                "capacity": 0.8,
                "unit": "доля",
                "measured": True,
            },
            {
                "resource": "inference:gpu:0",
                "used": 0.66,
                "capacity": 0.8,
                "unit": "доля",
                "measured": True,
            },
        ],
        "inputs_digest": "b" * 64,
    },
    "BenchResult": {
        "id": "bench-20261019-080000",
        "started_at": T0,
        "finished_at": T2,
        "hardware": {
            "hostname": "qoima-srv-01",
            "cpu_model": "AMD EPYC 9124 16-Core Processor",
            "cpu_cores": 32,
            "ram_total_mb": 128000,
            "gpus": [
                {
                    "index": 0,
                    "name": "NVIDIA L40S",
                    "vram_total_mb": 46068,
                    "nvdec_available": True,
                    "driver_version": "580.95",
                    "cuda_version": "13.0",
                }
            ],
            "onnxruntime": "1.31.0",
            "providers": [
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ],
            "libavcodec": "62.11.100",
            "pipeline_version": "0.4.0",
        },
        "repeats": 3,
        "decode": [
            {
                "codec": "h264",
                "width": 640,
                "height": 360,
                "source_fps": 15.0,
                "backend": "nvdec",
                "gpu_index": 0,
                "parallel_streams": 32,
                "fps_total": 2400.0,
                "cv": 0.03,
                "vram_per_stream_mb": 60,
                "source": "camera",
                "camera_ids": ["cam-07", "cam-08"],
            }
        ],
        "models": [
            {
                "model": "rfdetr-person",
                "version": "rfdetr-person-1.11.2+nano",
                "runtime": "tensorrt",
                "providers": [
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
                "batch": 8,
                "latency_p50_ms": 9.5,
                "latency_p95_ms": 11.2,
                "throughput": 840.0,
                "cv": 0.04,
                "vram_mb": 900,
            }
        ],
        "vlm": [],
        "live_load": {"cameras": 0, "decoded_fps": 0.0, "inferred_fps": 0.0},
        "warnings": [],
    },
    "ComponentHealth": {
        "component": "postgres",
        "kind": "database",
        "status": "green",
        "message": "Postgres доступен, схема актуальна",
        "checked_at": T0,
        "latency_ms": 3.2,
        "details": {"revision": "0001"},
    },
    "Event": {
        "id": EVENT_ID,
        "type": "fall",
        "camera_id": "cam-07",
        "zone_id": None,
        "track_ids": [42],
        "started_at": T0,
        "detected_at": T1,
        "detector_score": 0.71,
        "routing": "event_with_arbiter",
        "model_version": "rf-detr-person-1.0.0+rtmpose-1.0.0",
        "evidence": [FRAME],
        "area": "line-3",
        "synthetic": None,
    },
    "EventMedia": {
        "event_id": EVENT_ID,
        "camera_id": "cam-07",
        "clip": {
            "blob_key": f"clips/cam-07/2026-10-19/{EVENT_ID}.mp4",
            "starts_at": "2026-10-19T07:59:44+05:00",
            "ends_at": "2026-10-19T08:00:25+05:00",
            "codec": "h264",
        },
        "keyframes": [FRAME],
        "error": None,
    },
    "PipelineCommand": {
        "event_id": EVENT_ID,
        "kind": "inject_event",
        "camera_id": "cam-07",
        "event_type": "fall",
        "synthetic": "selftest",
        "zone_id": "mon-line-3",
        "issued_at": T0,
    },
    "Feedback": {
        "id": "33333333-4444-4555-8666-777777777777",
        "incident_id": INCIDENT_ID,
        "verdict": "false_alarm",
        "reason_code": "repair_lying",
        "comment": "Слесарь ремонтировал конвейер лёжа",
        "author_id": "operator:petrova",
        "created_at": T2,
    },
    "FloorCalibration": {
        "camera_id": "cam-07",
        "image_points": [
            {"x": 0.2, "y": 0.9},
            {"x": 0.8, "y": 0.9},
            {"x": 0.7, "y": 0.5},
            {"x": 0.3, "y": 0.5},
        ],
        "known_distance": {"point_a": 0, "point_b": 1, "meters": 4.0},
        "homography": None,
        "snapshot_key": "frames/cam-07/calibration.jpg",
        "calibrated_at": T0,
    },
    "Incident": {
        "id": INCIDENT_ID,
        "type": "fall",
        "state": "ACKNOWLEDGED",
        "severity": "critical",
        "camera_ids": ["cam-07", "cam-08"],
        "event_ids": [EVENT_ID],
        "dedup_key": "fall:zone-press-1",
        "opened_at": T1,
        "acknowledged_at": T2,
        "acknowledged_by": "operator:petrova",
        "clip": {
            "blob_key": f"clips/2026/10/19/{INCIDENT_ID}.mp4",
            "starts_at": "2026-10-19T07:59:55+05:00",
            "ends_at": "2026-10-19T08:00:25+05:00",
            "codec": "h264",
        },
        "keyframes": [FRAME],
        "vlm_verdict": VERDICT,
        "area": "line-3",
        "escalation_level": 1,
    },
    "ModelManifest": {
        "name": "helmet-detector",
        "version": "1.2.0",
        "task_type": "detection",
        "framework": "rf-detr",
        "license": "Apache-2.0",
        "weights_sha256": "a" * 64,
        "source": "trained",
        "training_task_id": "44444444-5555-4666-8777-888888888888",
        "golden_set_version": "helmet-gs-v3",
        "metrics": {"f1_macro": 0.91, "ece": 0.04},
        "status": "shadow",
        "created_at": T0,
        "runtime": {
            "format": "onnx",
            "input_name": "input",
            "input_width": 512,
            "input_height": 512,
            "color": "rgb",
            "mean": [0.485, 0.456, 0.406],
            "std": [0.229, 0.224, 0.225],
            "scale": 255.0,
            "outputs": ["dets", "labels"],
            "dynamic_batch": True,
            "decoder": "rfdetr",
            "class_ids": {"helmet": 1, "no_helmet": 2},
        },
    },
    "QualityBaseline": {
        "sharpness": 412.5,
        "brightness": 118.0,
        "noise": 3.1,
        "snapshot_key": "frames/cam-07/baseline.jpg",
        "captured_at": T0,
    },
    "StreamProbe": {
        "role": "sub",
        "url": "rtsp://10.0.5.17:554/Streaming/Channels/102",
        "codec": "h264",
        "width": 1280,
        "height": 720,
        "fps": 25.0,
        "bitrate_kbps": 1024,
        "gop_seconds": 2.0,
        "probed_at": T0,
    },
    "SystemAlert": {
        "id": SYSTEM_ALERT_ID,
        "kind": "camera_blur",
        "severity": "warning",
        "component": "camera-health",
        "camera_id": "cam-07",
        "message": "Камера 7: расфокус, резкость упала на 65% от базовой — проверьте объектив",
        "details": {"issues": ["blur"]},
        "raised_at": T0,
        "acknowledged_at": T1,
        "acknowledged_by": "site_admin:sergeev",
    },
    "SystemHealth": {
        "status": "yellow",
        "components": [
            {
                "component": "postgres",
                "kind": "database",
                "status": "green",
                "message": "ok",
                "checked_at": T0,
                "latency_ms": 2.0,
                "details": {},
            },
            {
                "component": "cameras",
                "kind": "cameras",
                "status": "yellow",
                "message": "Камеры не добавлены",
                "checked_at": T0,
                "latency_ms": 1.0,
                "details": {},
            },
            {
                "component": "llm",
                "kind": "llm",
                "status": "not_deployed",
                "message": "Фаза 4",
                "checked_at": T0,
                "latency_ms": None,
                "details": {},
            },
        ],
        "generated_at": T0,
    },
    "TaskSpec": {
        "id": "55555555-6666-4777-8888-999999999999",
        "title": "Каска / без каски на линии 3",
        "request_text": "Хочу видеть, кто заходит на линию 3 без каски",
        "task_type": "detection",
        "camera_ids": ["cam-07"],
        "roi": SQUARE,
        "target_fps": 2,
        "classes": [
            {"name": "helmet", "definition": "Защитная каска на голове человека"},
            {
                "name": "no_helmet",
                "definition": "Голова человека без каски; капюшон и кепка — тоже no_helmet",
            },
        ],
        "success_criteria": [
            {"metric": "recall", "comparator": "gte", "threshold": 0.9},
            {"metric": "ece", "comparator": "lte", "threshold": 0.05},
        ],
        "feasibility": {
            "verdict": "feasible_with_limitations",
            "reasoning": "Головы занимают ~40 px на substream; ночью точность ниже",
            "alternatives": ["Добавить камеру на входе линии 3 на высоте 2.5 м"],
        },
        "target_samples_per_class": 500,
        "gpu_hours_budget": 6,
        "created_at": T0,
    },
    "ToolCallAudit": {
        "id": "66666666-7777-4888-8999-aaaaaaaaaaaa",
        "at": T3,
        "agent": "chat",
        "on_behalf_of": "site_admin:sergeev",
        "tool": "propose_zone_change",
        "permission": "propose",
        "arguments": {"camera_id": "cam-07", "polygon": SQUARE},
        "status": "awaiting_confirmation",
    },
    "VLMVerdict": VERDICT,
    "CameraRegistryEntry": {
        "id": "cam-07",
        "name": "Линия упаковки, вид сверху",
        "enabled": True,
        "main_path": "cam-07/main",
        "sub_path": "cam-07/sub",
        "expected_fps": 15.0,
        "baseline": {
            "sharpness": 412.5,
            "brightness": 118.0,
            "noise": 3.1,
            "snapshot_key": "frames/cam-07/baseline.jpg",
            "captured_at": T0,
        },
        "scenarios": ["fall", "red_zone"],
        "zones": [
            {
                "id": "press-1",
                "camera_id": "cam-07",
                "name": "Пресс 1",
                "kind": "red_zone",
                "polygon": {
                    "points": [
                        {"x": 0.4, "y": 0.6},
                        {"x": 0.6, "y": 0.6},
                        {"x": 0.6, "y": 0.9},
                        {"x": 0.4, "y": 0.9},
                    ]
                },
                "dwell_seconds": 0.5,
                "area": "press-area",
            }
        ],
        "floor": None,
        "analysis_fps": 6.0,
    },
    "DiscoveredDevice": DEVICE,
    "DiscoveryJob": {
        "id": "77777777-8888-4999-8aaa-bbbbbbbbbbbb",
        "status": "done",
        "started_at": T0,
        "finished_at": T1,
        "subnets": ["10.0.5.0/24"],
        "hosts_total": 254,
        "hosts_scanned": 254,
        "devices": [DEVICE],
        "warnings": [],
    },
    "ProbeReport": {
        "host": "10.0.5.17",
        "channel": 1,
        "main": MAIN_PROBE,
        "sub": SUB_PROBE,
        "tried": [
            {"url": MAIN_PROBE["url"], "ok": True, "probe": MAIN_PROBE},
            {
                "url": "rtsp://10.0.5.17:554/Streaming/Channels/103",
                "ok": False,
                "error": "по этому адресу потока нет (404)",
            },
        ],
        "warnings": [],
        "probed_at": T0,
    },
    "DoctorReport": {
        "generated_at": T0,
        "hostname": "qoima-server",
        "version": "0.1.0",
        "outcomes": [
            {
                "name": "Драйвер NVIDIA",
                "level": "ok",
                "detail": "580.95.05, GPU: 1",
                "recommendation": "",
            },
            {
                "name": "Время",
                "level": "ok",
                "detail": "chrony: смещение 0.4 мс",
                "recommendation": "",
            },
        ],
        "gpus": [{"index": 0, "name": "NVIDIA L40S", "driver": "580.95.05", "vram_mb": 46068}],
    },
    "Zone": {
        "id": "zone-press-1",
        "camera_id": "cam-07",
        "name": "Пресс №1",
        "kind": "red_zone",
        "polygon": SQUARE,
        "dwell_seconds": 0.5,
        "work_schedule": [
            {
                "activity": "maintenance",
                "weekdays": [5],
                "start": "09:00",
                "end": "13:00",
                "note": "ППР по субботам",
            }
        ],
        "area": "press-area",
    },
}
