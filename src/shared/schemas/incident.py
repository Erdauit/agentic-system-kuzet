"""Инциденты и их жизненный цикл (SPEC.md §7)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, Identifier
from shared.schemas.event import SAFETY_EVENT_TYPES, EventType, FrameRef
from shared.schemas.vlm import ArbiterOutcome, VLMVerdict


class IncidentState(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    FALSE_ALARM = "FALSE_ALARM"


# OPEN → ACKNOWLEDGED → RESOLVED | FALSE_ALARM. Финальные состояния не меняются.
ALLOWED_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.OPEN: frozenset({IncidentState.ACKNOWLEDGED}),
    IncidentState.ACKNOWLEDGED: frozenset({IncidentState.RESOLVED, IncidentState.FALSE_ALARM}),
    IncidentState.RESOLVED: frozenset(),
    IncidentState.FALSE_ALARM: frozenset(),
}


def can_transition(current: IncidentState, target: IncidentState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClipRef(Contract):
    """Клип инцидента, вырезанный из GOP-буфера без перекодирования."""

    blob_key: str = Field(min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    codec: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("клип: ends_at должен быть позже starts_at")
        return self


class Incident(Contract):
    id: UUID
    type: EventType
    state: IncidentState
    severity: Severity
    zone_id: Identifier | None = None
    camera_ids: list[Identifier] = Field(min_length=1)
    event_ids: list[UUID] = Field(min_length=1)
    dedup_key: str = Field(min_length=1, max_length=200)
    opened_at: AwareDatetime
    acknowledged_at: AwareDatetime | None = None
    acknowledged_by: str | None = None
    resolved_at: AwareDatetime | None = None
    clip: ClipRef | None = None
    keyframes: list[FrameRef] = Field(default_factory=list, max_length=6)
    vlm_verdict: VLMVerdict | None = None
    arbiter_outcome: ArbiterOutcome | None = None

    @model_validator(mode="after")
    def _lifecycle_consistent(self) -> Self:
        if self.type not in SAFETY_EVENT_TYPES:
            raise ValueError(f"инцидент создаётся только для safety-событий, получено {self.type}")
        acked = self.state is not IncidentState.OPEN
        closed = self.state in (IncidentState.RESOLVED, IncidentState.FALSE_ALARM)
        if acked != (self.acknowledged_at is not None):
            raise ValueError(
                "acknowledged_at задаётся тогда и только тогда, когда инцидент подтверждён"
            )
        if acked and not self.acknowledged_by:
            raise ValueError("подтверждённый инцидент должен иметь acknowledged_by")
        if closed != (self.resolved_at is not None):
            raise ValueError("resolved_at задаётся только для RESOLVED и FALSE_ALARM")
        if self.acknowledged_at is not None and self.acknowledged_at < self.opened_at:
            raise ValueError("acknowledged_at раньше opened_at")
        if (
            self.resolved_at is not None
            and self.acknowledged_at is not None
            and self.resolved_at < self.acknowledged_at
        ):
            raise ValueError("resolved_at раньше acknowledged_at")
        return self
