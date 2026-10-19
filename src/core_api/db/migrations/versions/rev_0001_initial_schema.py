"""Начальная схема: пользователи, аудит, камеры, зоны, события, инциденты, фидбек, реестр моделей.

Revision ID: 0001
Revises:
Create Date: 2026-10-19

Сгенерирована autogenerate по core_api.db.models и дополнена вручную триггером, который делает
audit_log неизменяемым (SPEC.md §10: аудит-лог «кто, что, когда» нельзя переписать задним числом).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_IMMUTABLE_FUNCTION = """
CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log только для добавления: % запрещён', TG_OP;
END;
$$
"""

AUDIT_IMMUTABLE_TRIGGER = """
CREATE TRIGGER audit_log_immutable
BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()
"""


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=True),
        sa.Column("permission", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("confirmed_by", sa.String(length=120), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT (permission = 'act' AND status = 'ok' AND confirmed_by IS NULL)",
            name=op.f("ck_audit_log_act_confirmed"),
        ),
        sa.CheckConstraint(
            "kind <> 'tool_call' OR (permission IS NOT NULL AND status IS NOT NULL)",
            name=op.f("ck_audit_log_tool_call_fields"),
        ),
        sa.CheckConstraint(
            "kind IN ('user_action', 'tool_call')", name=op.f("ck_audit_log_kind_valid")
        ),
        sa.CheckConstraint(
            "permission IS NULL OR permission IN ('read', 'propose', 'act')",
            name=op.f("ck_audit_log_permission_valid"),
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('ok', 'denied', 'error', 'awaiting_confirmation', 'cancelled')",
            name=op.f("ck_audit_log_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(op.f("ix_audit_log_actor_at"), "audit_log", ["actor", "at"], unique=False)
    op.create_index(op.f("ix_audit_log_at"), "audit_log", ["at"], unique=False)
    op.create_table(
        "cameras",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("main_url", sa.Text(), nullable=False),
        sa.Column("sub_url", sa.Text(), nullable=True),
        sa.Column("credentials_secret", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "scenarios",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("probes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("baseline", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("calibration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('onvif', 'manual', 'import', 'simulator')",
            name=op.f("ck_cameras_source_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cameras")),
    )
    op.create_table(
        "models",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("framework", sa.String(length=60), nullable=False),
        sa.Column("license", sa.String(length=120), nullable=False),
        sa.Column("weights_sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("training_task_id", sa.Uuid(), nullable=True),
        sa.Column("golden_set_version", sa.String(length=64), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("promoted_by", sa.String(length=120), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('pretrained', 'qoima', 'trained')", name=op.f("ck_models_source_valid")
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'shadow', 'active', 'retired', 'rejected')",
            name=op.f("ck_models_status_valid"),
        ),
        sa.CheckConstraint(
            "task_type IN ('detection', 'segmentation', 'keypoints', 'anomaly', 'classification')",
            name=op.f("ck_models_task_type_valid"),
        ),
        sa.PrimaryKeyConstraint("name", "version", name=op.f("pk_models")),
    )
    op.create_index(
        "uq_models_one_active",
        "models",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('operator', 'shift_lead', 'site_admin', 'qoima_engineer')",
            name=op.f("ck_users_role_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "zones",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("polygon", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dwell_seconds", sa.Float(), nullable=True),
        sa.Column(
            "work_schedule",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dwell_seconds IS NULL OR kind = 'red_zone'", name=op.f("ck_zones_dwell_only_red_zone")
        ),
        sa.CheckConstraint("kind IN ('red_zone', 'monitoring')", name=op.f("ck_zones_kind_valid")),
        sa.ForeignKeyConstraint(
            ["camera_id"],
            ["cameras.id"],
            name=op.f("fk_zones_camera_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zones")),
    )
    op.create_index(op.f("ix_zones_camera_id"), "zones", ["camera_id"], unique=False)
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("camera_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=120), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clip", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "keyframes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("vlm_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("arbiter_outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "(state = 'OPEN') = (acknowledged_at IS NULL)",
            name=op.f("ck_incidents_ack_matches_state"),
        ),
        sa.CheckConstraint(
            "(state IN ('RESOLVED', 'FALSE_ALARM')) = (resolved_at IS NOT NULL)",
            name=op.f("ck_incidents_resolved_matches_state"),
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')",
            name=op.f("ck_incidents_severity_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'FALSE_ALARM')",
            name=op.f("ck_incidents_state_valid"),
        ),
        sa.CheckConstraint(
            "type IN ('fall', 'red_zone', 'smoke_fire')", name=op.f("ck_incidents_type_valid")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_incidents_zone_id_zones"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(
        op.f("ix_incidents_dedup_key_opened_at"),
        "incidents",
        ["dedup_key", "opened_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_incidents_state_opened_at"), "incidents", ["state", "opened_at"], unique=False
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column(
            "track_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detector_score", sa.Float(), nullable=False),
        sa.Column("routing", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=120), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "routing IN ('event', 'event_with_arbiter')", name=op.f("ck_events_routing_valid")
        ),
        sa.CheckConstraint(
            "type IN ('fall', 'red_zone', 'smoke_fire', 'fall_recovered')",
            name=op.f("ck_events_type_valid"),
        ),
        sa.CheckConstraint(
            "detected_at >= started_at", name=op.f("ck_events_detected_after_start")
        ),
        sa.CheckConstraint(
            "detector_score >= 0 AND detector_score <= 1", name=op.f("ck_events_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["camera_id"],
            ["cameras.id"],
            name=op.f("fk_events_camera_id_cameras"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_events_incident_id_incidents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name=op.f("fk_events_zone_id_zones"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index(
        op.f("ix_events_camera_id_detected_at"),
        "events",
        ["camera_id", "detected_at"],
        unique=False,
    )
    op.create_index(op.f("ix_events_incident_id"), "events", ["incident_id"], unique=False)
    op.create_index(
        op.f("ix_events_type_detected_at"), "events", ["type", "detected_at"], unique=False
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("author_id", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(verdict = 'false_alarm') = (reason_code IS NOT NULL)",
            name=op.f("ck_feedback_reason_for_false_alarm"),
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('steam', 'welding', 'repair_lying', 'reflection', 'other')",
            name=op.f("ck_feedback_reason_code_valid"),
        ),
        sa.CheckConstraint(
            "verdict IN ('confirmed', 'false_alarm')", name=op.f("ck_feedback_verdict_valid")
        ),
        sa.CheckConstraint(
            "comment IS NULL OR char_length(comment) <= 1000",
            name=op.f("ck_feedback_comment_length"),
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_feedback_incident_id_incidents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_incident_id"), "feedback", ["incident_id"], unique=False)
    op.execute(AUDIT_IMMUTABLE_FUNCTION)
    op.execute(AUDIT_IMMUTABLE_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable()")
    op.drop_index(op.f("ix_feedback_incident_id"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_index(op.f("ix_events_type_detected_at"), table_name="events")
    op.drop_index(op.f("ix_events_incident_id"), table_name="events")
    op.drop_index(op.f("ix_events_camera_id_detected_at"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_incidents_state_opened_at"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_dedup_key_opened_at"), table_name="incidents")
    op.drop_table("incidents")
    op.drop_index(op.f("ix_zones_camera_id"), table_name="zones")
    op.drop_table("zones")
    op.drop_table("users")
    op.drop_index(
        "uq_models_one_active", table_name="models", postgresql_where=sa.text("status = 'active'")
    )
    op.drop_table("models")
    op.drop_table("cameras")
    op.drop_index(op.f("ix_audit_log_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_actor_at"), table_name="audit_log")
    op.drop_table("audit_log")
