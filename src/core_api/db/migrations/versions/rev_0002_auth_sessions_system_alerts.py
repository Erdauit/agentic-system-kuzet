"""Фаза 1: пароли и сессии пользователей, системные алерты, сведения ONVIF о камерах.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_sessions")),
    )
    op.create_index(op.f("ix_sessions_expires_at"), "sessions", ["expires_at"], unique=False)
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "system_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("component", sa.String(length=80), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=True),
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=120), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('camera_down', 'camera_frozen', 'camera_black', 'camera_overexposed', 'camera_blur', 'camera_occluded', 'camera_view_shift', 'component_down', 'disk_low', 'time_unsynced', 'canary_failed', 'vlm_degraded')",
            name=op.f("ck_system_alerts_kind_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('warning', 'critical')", name=op.f("ck_system_alerts_severity_valid")
        ),
        sa.CheckConstraint(
            "(acknowledged_at IS NULL) = (acknowledged_by IS NULL)",
            name=op.f("ck_system_alerts_ack_fields_together"),
        ),
        sa.ForeignKeyConstraint(
            ["camera_id"],
            ["cameras.id"],
            name=op.f("fk_system_alerts_camera_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_alerts")),
    )
    op.create_index(
        op.f("ix_system_alerts_raised_at"), "system_alerts", ["raised_at"], unique=False
    )
    op.create_index(
        "uq_system_alerts_active",
        "system_alerts",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL"),
    )
    op.add_column("cameras", sa.Column("vendor", sa.String(length=60), nullable=True))
    op.add_column("cameras", sa.Column("model", sa.String(length=120), nullable=True))
    op.add_column("cameras", sa.Column("onvif_xaddr", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "users",
        sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_logins")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "password_hash")
    op.drop_column("cameras", "onvif_xaddr")
    op.drop_column("cameras", "model")
    op.drop_column("cameras", "vendor")
    op.drop_index(
        "uq_system_alerts_active",
        table_name="system_alerts",
        postgresql_where=sa.text("cleared_at IS NULL"),
    )
    op.drop_index(op.f("ix_system_alerts_raised_at"), table_name="system_alerts")
    op.drop_table("system_alerts")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_expires_at"), table_name="sessions")
    op.drop_table("sessions")
