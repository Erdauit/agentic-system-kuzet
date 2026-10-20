"""Фаза 2: доставки алертов (at-least-once), эскалация инцидентов, участки зон, служебные события.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Значения зафиксированы на момент ревизии (миграция не должна зависеть от будущих enum).
_KINDS_0002_LIST = [
    "camera_down",
    "camera_frozen",
    "camera_black",
    "camera_overexposed",
    "camera_blur",
    "camera_occluded",
    "camera_view_shift",
    "component_down",
    "disk_low",
    "time_unsynced",
    "canary_failed",
    "vlm_degraded",
]
_KINDS_0002 = ", ".join(f"'{k}'" for k in _KINDS_0002_LIST)
_KINDS_0003 = ", ".join(f"'{k}'" for k in [*_KINDS_0002_LIST, "alert_delivery_failed"])


def upgrade() -> None:
    op.create_table(
        "alert_deliveries",
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("stream", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("system_alert_id", sa.Uuid(), nullable=True),
        sa.Column("escalation_level", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(stream = 'safety') = (incident_id IS NOT NULL) AND (stream = 'system') = (system_alert_id IS NOT NULL)",
            name=op.f("ck_alert_deliveries_stream_reference"),
        ),
        sa.CheckConstraint(
            "channel IN ('web_ui', 'telegram', 'local_relay', 'opcua')",
            name=op.f("ck_alert_deliveries_channel_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'retrying', 'failed')",
            name=op.f("ck_alert_deliveries_status_valid"),
        ),
        sa.CheckConstraint(
            "stream IN ('safety', 'system')", name=op.f("ck_alert_deliveries_stream_valid")
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_alert_deliveries_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["system_alert_id"],
            ["system_alerts.id"],
            name=op.f("fk_alert_deliveries_system_alert_id_system_alerts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("idempotency_key", name=op.f("pk_alert_deliveries")),
    )
    op.create_index(
        op.f("ix_alert_deliveries_incident_id"), "alert_deliveries", ["incident_id"], unique=False
    )
    op.create_index(
        op.f("ix_alert_deliveries_status_next_attempt_at"),
        "alert_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.add_column("events", sa.Column("area", sa.String(length=64), nullable=True))
    op.add_column("events", sa.Column("synthetic", sa.String(length=16), nullable=True))
    op.add_column("incidents", sa.Column("area", sa.String(length=64), nullable=True))
    op.add_column("incidents", sa.Column("synthetic", sa.String(length=16), nullable=True))
    op.add_column(
        "incidents",
        sa.Column("escalation_level", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "incidents", sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "incidents", sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("zones", sa.Column("area", sa.String(length=64), nullable=True))
    # CHECK-ограничения для новых колонок существующих таблиц autogenerate не видит — вручную.
    for table in ("events", "incidents"):
        op.create_check_constraint(
            op.f(f"ck_{table}_synthetic_valid"),
            table,
            "synthetic IS NULL OR synthetic IN ('selftest', 'canary')",
        )
    op.drop_constraint(op.f("ck_system_alerts_kind_valid"), "system_alerts", type_="check")
    op.create_check_constraint(
        op.f("ck_system_alerts_kind_valid"), "system_alerts", f"kind IN ({_KINDS_0003})"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_system_alerts_kind_valid"), "system_alerts", type_="check")
    op.create_check_constraint(
        op.f("ck_system_alerts_kind_valid"), "system_alerts", f"kind IN ({_KINDS_0002})"
    )
    for table in ("events", "incidents"):
        op.drop_constraint(op.f(f"ck_{table}_synthetic_valid"), table, type_="check")
    op.drop_column("zones", "area")
    op.drop_column("incidents", "last_event_at")
    op.drop_column("incidents", "last_escalated_at")
    op.drop_column("incidents", "escalation_level")
    op.drop_column("incidents", "synthetic")
    op.drop_column("incidents", "area")
    op.drop_column("events", "synthetic")
    op.drop_column("events", "area")
    op.drop_index(op.f("ix_alert_deliveries_status_next_attempt_at"), table_name="alert_deliveries")
    op.drop_index(op.f("ix_alert_deliveries_incident_id"), table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
