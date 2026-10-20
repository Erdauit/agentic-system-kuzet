"""Маршрутизация по уверенности (SPEC.md §5.10).

P > T_high — событие; T_low ≤ P ≤ T_high — событие и запрос арбитру (VLM, Фаза 4; до неё
событие уходит как есть — fail-open); P < T_low — события нет. Ширину серой зоны ограничивает
ёмкость VLM — её считает планировщик мощности (Фаза 3).
"""

from __future__ import annotations

from shared.schemas import RoutingDecision, ThresholdPair


def route(score: float, thresholds: ThresholdPair) -> RoutingDecision | None:
    if score > thresholds.t_high:
        return RoutingDecision.EVENT
    if score >= thresholds.t_low:
        return RoutingDecision.EVENT_WITH_ARBITER
    return None
