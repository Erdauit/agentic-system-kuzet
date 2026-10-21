"""Фаза 3: планы мощности и FPS анализа камеры из применённого плана.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capacity_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("benchmark_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.String(length=120), nullable=True),
        sa.CheckConstraint(
            "(status = 'draft') = (applied_at IS NULL)",
            name=op.f("ck_capacity_plans_applied_matches_status"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'superseded')",
            name=op.f("ck_capacity_plans_status_valid"),
        ),
        sa.CheckConstraint(
            "verdict IN ('fits', 'fits_with_limitations', 'does_not_fit')",
            name=op.f("ck_capacity_plans_verdict_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capacity_plans")),
    )
    op.create_index(
        op.f("ix_capacity_plans_created_at"), "capacity_plans", ["created_at"], unique=False
    )
    op.create_index(
        "uq_capacity_plans_one_active",
        "capacity_plans",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column("cameras", sa.Column("analysis_fps", sa.Float(), nullable=True))
    # CHECK для новой колонки существующей таблицы autogenerate не видит — вручную.
    op.create_check_constraint(
        op.f("ck_cameras_analysis_fps_range"),
        "cameras",
        "analysis_fps IS NULL OR (analysis_fps > 0 AND analysis_fps <= 30)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_cameras_analysis_fps_range"), "cameras", type_="check")
    op.drop_column("cameras", "analysis_fps")
    op.drop_index(
        "uq_capacity_plans_one_active",
        table_name="capacity_plans",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index(op.f("ix_capacity_plans_created_at"), table_name="capacity_plans")
    op.drop_table("capacity_plans")
