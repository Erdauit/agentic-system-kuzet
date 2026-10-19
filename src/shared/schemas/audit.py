"""Роли, уровни прав инструментов и аудит (SPEC.md §2.4, §3.3, §8.2, §10)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from shared.schemas.common import Contract


class Role(StrEnum):
    OPERATOR = "operator"
    SHIFT_LEAD = "shift_lead"
    SITE_ADMIN = "site_admin"
    QOIMA_ENGINEER = "qoima_engineer"


class PermissionLevel(StrEnum):
    READ = "read"
    PROPOSE = "propose"
    ACT = "act"  # выполняется только после явного подтверждения человеком


class ToolCallStatus(StrEnum):
    OK = "ok"
    DENIED = "denied"
    ERROR = "error"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CANCELLED = "cancelled"


class ToolCallAudit(Contract):
    """Запись о вызове инструмента агентом. Пишется для каждого вызова, включая отказы."""

    id: UUID
    at: AwareDatetime
    agent: str = Field(min_length=1, max_length=80)
    on_behalf_of: str | None = Field(
        default=None, description="Пользователь, от имени которого действует агент"
    )
    tool: str = Field(min_length=1, max_length=80)
    permission: PermissionLevel
    arguments: dict[str, JsonValue]
    status: ToolCallStatus
    confirmed_by: str | None = None
    result_summary: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _act_requires_human(self) -> Self:
        if (
            self.permission is PermissionLevel.ACT
            and self.status is ToolCallStatus.OK
            and not self.confirmed_by
        ):
            raise ValueError("действие уровня act выполнено без подтверждения человеком")
        return self


class AuditAction(StrEnum):
    LOGIN = "login"
    CONFIG_CHANGE = "config_change"
    INCIDENT_TRANSITION = "incident_transition"
    FEEDBACK = "feedback"
    MODEL_PROMOTE = "model_promote"
    MODEL_ROLLBACK = "model_rollback"
    PROPOSAL_DECISION = "proposal_decision"
    SYSTEM = "system"


class AuditRecord(Contract):
    """Действие человека или системы (не агента — для агентов см. ToolCallAudit)."""

    id: UUID
    at: AwareDatetime
    actor: str = Field(min_length=1, max_length=120)
    action: AuditAction
    target: str | None = Field(default=None, max_length=200)
    details: dict[str, JsonValue] = Field(default_factory=dict)
