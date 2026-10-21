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
    Integer,
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
    AlertStream,
    CameraSource,
    ChannelKind,
    DeliveryStatus,
    EventType,
    FeedbackVerdict,
    FitVerdict,
    IncidentState,
    ModelSource,
    ModelStatus,
    PermissionLevel,
    PlanStatus,
    ReasonCode,
    Role,
    RoutingDecision,
    Severity,
    SyntheticKind,
    SystemAlertKind,
    SystemAlertSeverity,
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
    # scrypt-хеш (core_api.auth.passwords). NULL — вход по паролю невозможен.
    password_hash: Mapped[str | None] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    failed_logins: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (enum_check("role", Role),)


class Session(Base):
    """Серверная сессия. В БД хранится только sha256 токена: утечка таблицы не даёт входа."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = _tz_now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = _tz_now()
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (Index(None, "user_id"), Index(None, "expires_at"))


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
    vendor: Mapped[str | None] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(120))
    onvif_xaddr: Mapped[str | None] = mapped_column(Text)
    # FPS анализа из применённого плана мощности; NULL — целевой FPS сценариев.
    analysis_fps: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _tz_now()
    updated_at: Mapped[datetime] = _tz_now()

    __table_args__ = (
        enum_check("source", CameraSource),
        CheckConstraint(
            "analysis_fps IS NULL OR (analysis_fps > 0 AND analysis_fps <= 30)",
            name="analysis_fps_range",
        ),
    )


class SystemAlert(Base):
    """Системные алерты (здоровье камер и компонентов). Отдельно от инцидентов безопасности."""

    __tablename__ = "system_alerts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(80))
    camera_id: Mapped[str | None] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    dedup_key: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=JSONB_EMPTY_OBJECT)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        enum_check("kind", SystemAlertKind),
        enum_check("severity", SystemAlertSeverity),
        CheckConstraint(
            "(acknowledged_at IS NULL) = (acknowledged_by IS NULL)", name="ack_fields_together"
        ),
        # Одновременно активен не больше одного алерта с тем же ключом (вид:компонент:камера).
        Index(
            "uq_system_alerts_active",
            "dedup_key",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
        ),
        Index(None, "raised_at"),
    )


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))
    polygon: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dwell_seconds: Mapped[float | None] = mapped_column(Float)
    work_schedule: Mapped[list[Any]] = mapped_column(server_default=JSONB_EMPTY_LIST)
    area: Mapped[str | None] = mapped_column(String(64))
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
    area: Mapped[str | None] = mapped_column(String(64))
    synthetic: Mapped[str | None] = mapped_column(String(16))
    escalation_level: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"type IN ({sql_in(_SAFETY_TYPES)})", name="type_valid"),
        enum_check("synthetic", SyntheticKind, nullable=True),
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
    area: Mapped[str | None] = mapped_column(String(64))
    synthetic: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = _tz_now()

    __table_args__ = (
        enum_check("type", EventType),
        enum_check("synthetic", SyntheticKind, nullable=True),
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


class AlertDeliveryRow(Base):
    """Доставка алерта в канал: at-least-once с идемпотентным ключом и ретраями (SPEC.md §7).

    Ключ — sha256(поток|инцидент|канал|получатель|уровень эскалации): повторная постановка той же
    доставки не создаёт дубль у получателя.
    """

    __tablename__ = "alert_deliveries"

    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    stream: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(200))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE")
    )
    system_alert_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("system_alerts.id", ondelete="CASCADE")
    )
    escalation_level: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _tz_now()
    next_attempt_at: Mapped[datetime] = _tz_now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        enum_check("stream", AlertStream),
        enum_check("channel", ChannelKind),
        enum_check("status", DeliveryStatus),
        CheckConstraint(
            "(stream = 'safety') = (incident_id IS NOT NULL) AND (stream = 'system') = (system_alert_id IS NOT NULL)",
            name="stream_reference",
        ),
        Index(None, "status", "next_attempt_at"),
        Index(None, "incident_id"),
    )


class CapacityPlanRow(Base):
    """Планы мощности (Фаза 3). Активен не больше одного; применяет только человек."""

    __tablename__ = "capacity_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = _tz_now()
    created_by: Mapped[str] = mapped_column(String(120))
    benchmark_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(32))
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        enum_check("status", PlanStatus),
        enum_check("verdict", FitVerdict),
        CheckConstraint("(status = 'draft') = (applied_at IS NULL)", name="applied_matches_status"),
        Index(
            "uq_capacity_plans_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(None, "created_at"),
    )
