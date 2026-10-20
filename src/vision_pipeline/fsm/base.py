"""Общие типы FSM."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.schemas import EventType


@dataclass(frozen=True)
class FsmSignal:
    """Сработка FSM: из неё hot path делает Event (маршрутизация, кадры, клип)."""

    type: EventType
    started_at: float  # монотонные секунды: начало ситуации (момент падения, вход в зону)
    detected_at: float  # момент перехода в ALERT
    score: float  # 0..1 — уверенность, идёт в маршрутизатор
    track_ids: list[int] = field(default_factory=list)
    zone_id: str | None = None
    details: dict[str, float | str | bool] = field(default_factory=dict)
