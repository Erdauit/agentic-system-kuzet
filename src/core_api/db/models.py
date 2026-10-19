"""ORM-модели core-api (SQLAlchemy 2). Схема БД меняется только миграциями Alembic.

Перечисления хранятся как text + CHECK (а не как типы ENUM Postgres): добавление значения —
обычная миграция без ALTER TYPE. Допустимые значения берутся из контрактов shared.schemas;
тест tests/unit/test_db_models.py следит, что CHECK и контракты не разошлись.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.schemas import (
    SAFETY_EVENT_TYPES,
    CameraSource,
    EventType,
    FeedbackVerdict,
    IncidentState,
    ModelSource,
    ModelStatus,
    PermissionLevel,
    ReasonCode,
    Role,
    RoutingDecision,
    Severity,
    TaskType,
    ToolCallStatus,
    ZoneKind,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def sql_in(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def enum_check(column: str, enum: type[StrEnum], *, nullable: bool = False) -> CheckConstraint:
    condition = f"{column} IN ({sql_in([e.value for e in enum])})"
    if nullable:
        condition = f"{column} IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"{column}_valid")


def _tz_now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


JSONB_EMPTY_LIST = text("'[]'::jsonb")
JSONB_EMPTY_OBJECT = text("'{}'::jsonb")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _tz_now()

    __table_args__ = (enum_check("role", Role),)


class AuditLog(Base):
    """Аудит действий людей и всех вызовов инструментов агентами (SPEC.md §3.3, §10).

    Таблица только для добавления: UPDATE и DELETE запрещены триггером (см. миграцию 0001).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _tz_now()
    kind: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str | None] = mapped_column(String(200))
    permission: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str | None] = mapped_column(String(32))
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=JSONB_EMPTY_OBJECT)

    __table_args__ = (
        CheckConstraint("kind IN ('user_action', 'tool_call')", name="kind_valid"),
        enum_check("permission", PermissionLevel, nullable=True),
        enum_check("status", ToolCallStatus, nullable=True),
        CheckConstraint(
            "kind <> 'tool_call' OR (permission IS NOT NULL AND status IS NOT NULL)",
            name="tool_call_fields",
        ),
        # Человек в контуре на уровне БД: act со статусом ok без подтверждения записать нельзя.
        CheckConstraint(
            "NOT (permission = 'act' AND status = 'ok' AND confirmed_by IS NULL)",
            name="act_confirmed",
        ),
        Index(None, "at"),
        Index(None, "actor", "at"),
    )


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(16))
    main_url: Mapped[str] = mapped_column(Text)
    sub_url: Mapped[str | None] = mapped_column(Text)
    credentials_secret: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    scenarios: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    location: Mapped[str | None] = mapped_column(String(200))
    probes: Mapped[list[Any] | None] = mapped_column(JSONB)
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    calibration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _tz_now()
    updated_at: Mapped[datetime] = _tz_now()

    __table_args__ = (enum_check("source", CameraSource),)


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))
    polygon: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dwell_seconds: Mapped[float | None] = mapped_column(Float)
    work_schedule: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    created_at: Mapped[datetime] = _tz_now()
    updated_at: Mapped[datetime] = _tz_now()

    __table_args__ = (
        enum_check("kind", ZoneKind),
        CheckConstraint("dwell_seconds IS NULL OR kind = 'red_zone'", name="dwell_only_red_zone"),
        Index(None, "camera_id"),
    )


_SAFETY_TYPES = sorted(t.value for t in SAFETY_EVENT_TYPES)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(16))
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"))
    camera_ids: Mapped[list[Any]] = mapped_column(JSONB)
    dedup_key: Mapped[str] = mapped_column(String(200))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clip: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    keyframes: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    vlm_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    arbiter_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(f"type IN ({sql_in(_SAFETY_TYPES)})", name="type_valid"),
        enum_check("state", IncidentState),
        enum_check("severity", Severity),
        CheckConstraint("(state = 'OPEN') = (acknowledged_at IS NULL)", name="ack_matches_state"),
        CheckConstraint(
            "(state IN ('RESOLVED', 'FALSE_ALARM')) = (resolved_at IS NOT NULL)",
            name="resolved_matches_state",
        ),
        Index(None, "state", "opened_at"),
        Index(None, "dedup_key", "opened_at"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="RESTRICT"))
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zones.id", ondelete="SET NULL"))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="SET NULL")
    )
    track_ids: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detector_score: Mapped[float] = mapped_column(Float)
    routing: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(120))
    evidence: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    created_at: Mapped[datetime] = _tz_now()

    __table_args__ = (
        enum_check("type", EventType),
        enum_check("routing", RoutingDecision),
        CheckConstraint("detector_score >= 0 AND detector_score <= 1", name="score_range"),
        CheckConstraint("detected_at >= started_at", name="detected_after_start"),
        Index(None, "camera_id", "detected_at"),
        Index(None, "type", "detected_at"),
        Index(None, "incident_id"),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="RESTRICT"))
    verdict: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str | None] = mapped_column(String(32))
    # Комментарий оператора — данные, а не инструкции (SPEC.md §2.6).
    comment: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = _tz_now()

    __table_args__ = (
        enum_check("verdict", FeedbackVerdict),
        enum_check("reason_code", ReasonCode, nullable=True),
        CheckConstraint(
            "(verdict = 'false_alarm') = (reason_code IS NOT NULL)", name="reason_for_false_alarm"
        ),
        CheckConstraint("comment IS NULL OR char_length(comment) <= 1000", name="comment_length"),
        Index(None, "incident_id"),
    )


class Model(Base):
    """Реестр моделей (SPEC.md §3.3 «Модели», §9.1 п. 9)."""

    __tablename__ = "models"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32))
    framework: Mapped[str] = mapped_column(String(60))
    license: Mapped[str] = mapped_column(String(120))
    weights_sha256: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    training_task_id: Mapped[uuid.UUID | None] = mapped_column()
    golden_set_version: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=JSONB_EMPTY_OBJECT)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = _tz_now()
    promoted_by: Mapped[str | None] = mapped_column(String(120))
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        enum_check("task_type", TaskType),
        enum_check("source", ModelSource),
        enum_check("status", ModelStatus),
        # Не больше одной активной версии модели — откат переключает, а не добавляет.
        Index(
            "uq_models_one_active",
            "name",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
