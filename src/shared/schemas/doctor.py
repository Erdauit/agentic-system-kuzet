"""Отчёт `qoima doctor --report` (SPEC.md §6.3).

Контейнеры не видят драйвер, chrony и диски хоста, поэтому CLI пишет отчёт в каталог данных,
а core-api показывает его в мастере (шаг 2) и в разделе «Состояние системы» (время).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from shared.schemas.common import Contract


class DoctorLevel(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class DoctorOutcome(Contract):
    name: str = Field(min_length=1, max_length=120)
    level: DoctorLevel
    detail: str = Field(max_length=2000)
    recommendation: str = Field(default="", max_length=1000)


class DoctorGpu(Contract):
    index: int = Field(ge=0)
    name: str
    driver: str
    vram_mb: int = Field(gt=0)


class DoctorReport(Contract):
    generated_at: AwareDatetime
    hostname: str = Field(min_length=1, max_length=255)
    version: str
    outcomes: list[DoctorOutcome]
    gpus: list[DoctorGpu] = Field(default_factory=list)

    def outcome(self, name: str) -> DoctorOutcome | None:
        return next((o for o in self.outcomes if o.name == name), None)

    @property
    def verdict(self) -> DoctorLevel:
        levels = {o.level for o in self.outcomes}
        if DoctorLevel.FAIL in levels:
            return DoctorLevel.FAIL
        if DoctorLevel.WARN in levels:
            return DoctorLevel.WARN
        return DoctorLevel.OK
