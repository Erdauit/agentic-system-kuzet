"""Зоны, расписание работ и калибровка плоскости пола."""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract, DisplayName, Identifier, Point, Polygon


class ZoneKind(StrEnum):
    RED_ZONE = "red_zone"
    # Область контроля для падений и дыма/огня: ограничивает анализ и несёт расписание работ.
    MONITORING = "monitoring"


class WorkActivity(StrEnum):
    """Плановые работы, которые дают похожие на опасность картинки (SPEC.md §5.8, SmokeFireFSM)."""

    WELDING = "welding"
    STEAMING = "steaming"
    MAINTENANCE = "maintenance"
    OTHER = "other"


class WorkWindow(Contract):
    """Окно плановых работ. end < start означает переход через полночь (ночная смена)."""

    activity: WorkActivity
    weekdays: list[int] = Field(min_length=1, max_length=7, description="0 — понедельник")
    start: time
    end: time
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("weekdays: значения 0..6")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays: дни повторяются")
        if self.start == self.end:
            raise ValueError("окно работ нулевой длины")
        return self


class Zone(Contract):
    id: Identifier
    camera_id: Identifier
    name: DisplayName
    kind: ZoneKind
    polygon: Polygon
    dwell_seconds: float | None = Field(
        default=None, gt=0, description="Только для red_zone; None — значение из сценария"
    )
    work_schedule: list[WorkWindow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dwell_only_for_red_zone(self) -> Self:
        if self.dwell_seconds is not None and self.kind is not ZoneKind.RED_ZONE:
            raise ValueError("dwell_seconds задаётся только для red_zone")
        return self


class KnownDistance(Contract):
    """Известное расстояние между двумя точками калибровки — задаёт масштаб в метрах."""

    point_a: int = Field(ge=0, le=3)
    point_b: int = Field(ge=0, le=3)
    meters: float = Field(gt=0, le=500)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.point_a == self.point_b:
            raise ValueError("точки известного расстояния должны различаться")
        return self


class FloorCalibration(Contract):
    """Калибровка плоскости пола: 4 точки на полу → гомография (SPEC.md §3.2, шаг 5).

    homography заполняет vision-pipeline (geometry) после расчёта; без калибровки пайплайн
    использует fallback-эвристику, а UI показывает пониженную точность.
    """

    camera_id: Identifier
    image_points: list[Point] = Field(min_length=4, max_length=4)
    known_distance: KnownDistance | None = None
    homography: list[list[float]] | None = None
    snapshot_key: str = Field(min_length=1, description="Снапшот, на котором делалась калибровка")
    calibrated_at: AwareDatetime

    @model_validator(mode="after")
    def _homography_shape(self) -> Self:
        h = self.homography
        if h is not None and (len(h) != 3 or any(len(row) != 3 for row in h)):
            raise ValueError("homography: ожидается матрица 3×3")
        return self
