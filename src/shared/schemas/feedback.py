"""Фидбек оператора по инциденту (SPEC.md §7).

Комментарий — это данные, а не инструкции (SPEC.md §2.6): он никогда не вставляется в промпт
как команда, а только как цитируемый текст.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from shared.schemas.common import Contract


class FeedbackVerdict(StrEnum):
    CONFIRMED = "confirmed"  # 👍
    FALSE_ALARM = "false_alarm"  # 👎


class ReasonCode(StrEnum):
    """Справочник причин ложных срабатываний."""

    STEAM = "steam"
    WELDING = "welding"
    REPAIR_LYING = "repair_lying"
    REFLECTION = "reflection"
    OTHER = "other"


class Feedback(Contract):
    id: UUID
    incident_id: UUID
    verdict: FeedbackVerdict
    reason_code: ReasonCode | None = None
    comment: str | None = Field(default=None, max_length=1000)
    author_id: str = Field(min_length=1, max_length=120)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _reason_for_false_alarm(self) -> Self:
        if self.verdict is FeedbackVerdict.FALSE_ALARM and self.reason_code is None:
            raise ValueError("для ложного срабатывания нужен код причины")
        if self.verdict is FeedbackVerdict.CONFIRMED and self.reason_code is not None:
            raise ValueError("код причины указывается только для ложного срабатывания")
        return self
