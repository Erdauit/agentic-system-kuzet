"""Контрактные тесты всех схем shared.schemas (SPEC.md §13.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from shared.schemas import (
    CONTRACTS,
    ArbiterOutcome,
    CameraConfig,
    CameraHealth,
    CapacityPlan,
    Feedback,
    HealthStatus,
    Incident,
    IncidentState,
    ModelManifest,
    Polygon,
    PriorityChange,
    SystemHealth,
    ToolCallAudit,
    VLMVerdict,
    can_transition,
    idempotency_key,
    worst_status,
)
from shared.schemas.alert import AlertDelivery, AlertStream, ChannelKind
from shared.schemas.export import render, schema_for
from tests.contract.examples import EXAMPLES, INCIDENT_ID, VERDICT

SNAPSHOTS = Path(__file__).parent / "snapshots"


def _example(name: str, **overrides: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(EXAMPLES[name]))
    data.update(overrides)
    return data


def test_every_contract_has_example() -> None:
    assert set(EXAMPLES) == set(CONTRACTS)


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_example_validates_and_round_trips(name: str) -> None:
    model = CONTRACTS[name]
    obj = model.model_validate(EXAMPLES[name])
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_schema_snapshot_matches(name: str) -> None:
    path = SNAPSHOTS / f"{name}.schema.json"
    assert path.is_file(), f"нет снапшота {path.name}: выполните `make schemas`"
    assert path.read_text(encoding="utf-8") == render(schema_for(name)), (
        f"контракт {name} изменился — проверьте совместимость и обновите снапшот (`make schemas`)"
    )


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_unknown_fields_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        CONTRACTS[name].model_validate(_example(name, unexpected_field=1))


# ---------------------------------------------------------------- fail-open (SPEC.md §2.1)


def test_vlm_cannot_express_suppression() -> None:
    assert {c.value for c in PriorityChange} == {"NONE", "LOWER", "RAISE"}


def test_vlm_confidence_field_rejected() -> None:
    with pytest.raises(ValidationError):
        VLMVerdict.model_validate({**VERDICT, "confidence": 0.97})


@pytest.mark.parametrize(
    ("hazard", "change"),
    [("CRITICAL", "LOWER"), ("NONE", "RAISE")],
)
def test_inconsistent_verdict_detected(hazard: str, change: str) -> None:
    v = VLMVerdict.model_validate(
        {**VERDICT, "hazard_assessment": hazard, "suggested_priority_change": change}
    )
    assert v.consistency_problems()


def test_fail_open_keeps_priority() -> None:
    base = _example("ArbiterOutcome")
    base.pop("verdict")
    ok = ArbiterOutcome.model_validate(
        {**base, "fail_open_reason": "timeout", "applied_priority_change": "NONE"}
    )
    assert ok.applied_priority_change is PriorityChange.NONE
    with pytest.raises(ValidationError, match="fail-open"):
        ArbiterOutcome.model_validate(
            {**base, "fail_open_reason": "timeout", "applied_priority_change": "LOWER"}
        )


def test_shadow_mode_never_changes_priority() -> None:
    with pytest.raises(ValidationError, match="shadow"):
        ArbiterOutcome.model_validate(_example("ArbiterOutcome", mode="shadow"))
    ArbiterOutcome.model_validate(
        _example("ArbiterOutcome", mode="shadow", applied_priority_change="NONE")
    )


def test_inconsistent_verdict_not_applied() -> None:
    bad = {**VERDICT, "hazard_assessment": "CRITICAL", "suggested_priority_change": "LOWER"}
    with pytest.raises(ValidationError, match="несогласованный"):
        ArbiterOutcome.model_validate(
            _example("ArbiterOutcome", verdict=bad, applied_priority_change="LOWER")
        )


# ---------------------------------------------------------------- инциденты и алерты


def test_incident_transitions() -> None:
    assert can_transition(IncidentState.OPEN, IncidentState.ACKNOWLEDGED)
    assert can_transition(IncidentState.ACKNOWLEDGED, IncidentState.FALSE_ALARM)
    assert not can_transition(IncidentState.OPEN, IncidentState.RESOLVED)
    assert not can_transition(IncidentState.RESOLVED, IncidentState.OPEN)


def test_incident_lifecycle_fields_consistent() -> None:
    with pytest.raises(ValidationError, match="acknowledged_at"):
        Incident.model_validate(_example("Incident", state="OPEN"))
    with pytest.raises(ValidationError, match="safety"):
        Incident.model_validate(_example("Incident", type="fall_recovered"))


def test_delivery_idempotency_key_is_deterministic() -> None:
    a = idempotency_key(AlertStream.SAFETY, UUID(INCIDENT_ID), ChannelKind.WEB_UI, "ops", 1)
    b = idempotency_key(AlertStream.SAFETY, UUID(INCIDENT_ID), ChannelKind.WEB_UI, "ops", 1)
    c = idempotency_key(AlertStream.SAFETY, UUID(INCIDENT_ID), ChannelKind.WEB_UI, "ops", 2)
    assert a == b != c
    with pytest.raises(ValidationError, match="idempotency_key"):
        AlertDelivery.model_validate(_example("AlertDelivery", target="другой"))


def test_system_and_safety_streams_not_mixed() -> None:
    with pytest.raises(ValidationError, match="safety"):
        AlertDelivery.model_validate(_example("AlertDelivery", system_alert_id=INCIDENT_ID))


def test_feedback_false_alarm_requires_reason() -> None:
    with pytest.raises(ValidationError, match="код причины"):
        Feedback.model_validate(_example("Feedback", reason_code=None))


# ---------------------------------------------------------------- камеры и геометрия


def test_password_in_rtsp_url_rejected() -> None:
    with pytest.raises(ValidationError, match="пароль"):
        CameraConfig.model_validate(
            _example("CameraConfig", main_url="rtsp://admin:12345@10.0.5.17/stream")
        )


def test_dead_stream_must_be_down() -> None:
    with pytest.raises(ValidationError, match="down"):
        CameraHealth.model_validate(_example("CameraHealth", stream_alive=False))


def test_flag_without_issue_rejected() -> None:
    with pytest.raises(ValidationError, match="frozen"):
        CameraHealth.model_validate(_example("CameraHealth", frozen=True))


def test_self_intersecting_polygon_rejected() -> None:
    # Несимметричная «бабочка»: площадь по формуле шнурования ненулевая, рёбра пересекаются.
    bowtie = [
        {"x": 0.1, "y": 0.1},
        {"x": 0.9, "y": 0.9},
        {"x": 0.9, "y": 0.2},
        {"x": 0.1, "y": 0.6},
    ]
    with pytest.raises(ValidationError, match="самопересекается"):
        Polygon.model_validate({"points": bowtie})


# ---------------------------------------------------------------- план мощности и модели


def test_capacity_vram_over_budget_rejected() -> None:
    plan = _example("CapacityPlan")
    plan["vram"][0]["llm_mb"] = 40000
    with pytest.raises(ValidationError, match="VRAM"):
        CapacityPlan.model_validate(plan)


def test_capacity_vlm_load_limit() -> None:
    plan = _example("CapacityPlan")
    plan["vlm"]["expected_rps"] = 3.0  # 75% > 70%
    with pytest.raises(ValidationError, match="загрузка VLM"):
        CapacityPlan.model_validate(plan)


def test_capacity_degraded_cannot_claim_fits() -> None:
    with pytest.raises(ValidationError, match="fits_with_limitations"):
        CapacityPlan.model_validate(_example("CapacityPlan", verdict="fits"))


def test_trained_model_needs_golden_set_for_shadow() -> None:
    with pytest.raises(ValidationError, match="golden set"):
        ModelManifest.model_validate(_example("ModelManifest", golden_set_version=None))


def test_trained_model_promotion_needs_human() -> None:
    with pytest.raises(ValidationError, match="promoted_by"):
        ModelManifest.model_validate(_example("ModelManifest", status="active"))


def test_act_without_confirmation_rejected() -> None:
    with pytest.raises(ValidationError, match="act"):
        ToolCallAudit.model_validate(_example("ToolCallAudit", permission="act", status="ok"))


# ---------------------------------------------------------------- health


def test_worst_status_ignores_not_deployed_and_treats_unknown_as_bad() -> None:
    assert worst_status([HealthStatus.GREEN, HealthStatus.NOT_DEPLOYED]) is HealthStatus.GREEN
    assert worst_status([HealthStatus.YELLOW, HealthStatus.UNKNOWN]) is HealthStatus.UNKNOWN
    assert worst_status([HealthStatus.UNKNOWN, HealthStatus.RED]) is HealthStatus.RED
    assert worst_status([]) is HealthStatus.UNKNOWN


def test_system_health_status_must_be_worst() -> None:
    with pytest.raises(ValidationError, match="общий статус"):
        SystemHealth.model_validate(_example("SystemHealth", status="green"))
