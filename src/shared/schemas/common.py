"""Базовые типы для всех межсервисных контрактов."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class Contract(BaseModel):
    """Базовый класс контрактов.

    extra="forbid" — неизвестное поле является ошибкой: опечатки в конфиге и лишние поля
    в ответе LLM ловятся сразу, а не игнорируются молча.
    frozen=True — контракт описывает факт (событие, вердикт, план), а не изменяемое состояние.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# Человекочитаемые идентификаторы (камеры, зоны, модели): латиница в нижнем регистре, цифры, "_" и "-".
# Используются в путях BlobStore, метках Prometheus и URL, поэтому набор символов ограничен.
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_\-]{0,63}$")]

# Короткий человекочитаемый текст (названия, подписи в UI).
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=120)]

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]


class ScenarioKind(StrEnum):
    """Сценарии детекции (SPEC.md §5.8). Имена совпадают с файлами configs/scenarios/*.yaml."""

    FALL = "fall"
    RED_ZONE = "red_zone"
    SMOKE_FIRE = "smoke_fire"


class Language(StrEnum):
    RU = "ru"
    KK = "kk"


class Point(Contract):
    """Точка в нормализованных координатах кадра (0..1).

    Нормализация делает зоны независимыми от разрешения: полигон, нарисованный на снапшоте
    mainstream, корректно применяется к substream.
    """

    x: UnitFloat
    y: UnitFloat


class BBox(Contract):
    """Прямоугольник в нормализованных координатах кадра."""

    x1: UnitFloat
    y1: UnitFloat
    x2: UnitFloat
    y2: UnitFloat

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (self.x1 < self.x2 and self.y1 < self.y2):
            raise ValueError("bbox: должно быть x1 < x2 и y1 < y2")
        return self


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)


def _segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Строгое пересечение отрезков (касание концами не считается)."""
    d1 = _cross(q1, q2, p1)
    d2 = _cross(q1, q2, p2)
    d3 = _cross(p1, p2, q1)
    d4 = _cross(p1, p2, q2)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def polygon_area(points: list[Point]) -> float:
    """Площадь полигона по формуле шнурования (в долях площади кадра)."""
    n = len(points)
    s = 0.0
    for i in range(n):
        a, b = points[i], points[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return abs(s) / 2.0


# Минимальная площадь зоны — 0.01% кадра. Меньшие полигоны — почти наверняка случайный клик
# в редакторе, а не осмысленная зона.
MIN_POLYGON_AREA = 1e-4


class Polygon(Contract):
    """Простой (без самопересечений) полигон в нормализованных координатах."""

    points: list[Point] = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def _simple_and_non_degenerate(self) -> Self:
        pts = self.points
        if polygon_area(pts) < MIN_POLYGON_AREA:
            raise ValueError("полигон вырожден: площадь слишком мала")
        n = len(pts)
        for i in range(n):
            a1, a2 = pts[i], pts[(i + 1) % n]
            for j in range(i + 1, n):
                # соседние рёбра имеют общую вершину — их не проверяем
                if j == i or (j + 1) % n == i or (i + 1) % n == j:
                    continue
                b1, b2 = pts[j], pts[(j + 1) % n]
                if _segments_intersect(a1, a2, b1, b2):
                    raise ValueError("полигон самопересекается")
        return self
