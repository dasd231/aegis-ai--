"""Add distributed canary observations and rollback control.

Revision ID: 20260723_0007
Revises: 20260723_0006
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0007"
down_revision: str | None = "20260723_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "experiment_controls",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("canary_enabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=500), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "canary_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("experiment_group", sa.String(length=20), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("rag_version", sa.String(length=40), nullable=False),
        sa.Column("service_version", sa.String(length=80), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("timed_out", sa.Boolean(), nullable=False),
        sa.Column("tool_failed", sa.Boolean(), nullable=False),
        sa.Column("ttft_ms", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_canary_observations_created",
        "canary_observations",
        ["created_at"],
    )
    op.create_table(
        "shadow_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("stable_message_id", sa.String(length=36), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("rag_version", sa.String(length=40), nullable=False),
        sa.Column("service_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("stable_answer_sha256", sa.String(length=64), nullable=False),
        sa.Column("canary_answer_sha256", sa.String(length=64), nullable=True),
        sa.Column("canary_answer_chars", sa.Integer(), nullable=False),
        sa.Column("lexical_overlap", sa.Integer(), nullable=True),
        sa.Column("citation_valid", sa.Boolean(), nullable=False),
        sa.Column("side_effects_mocked", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shadow_observations_created", "shadow_observations", ["created_at"])
    op.create_index("ix_shadow_observations_stable_message_id", "shadow_observations", ["stable_message_id"])
    control = sa.table(
        "experiment_controls",
        sa.column("id", sa.String),
        sa.column("canary_enabled", sa.Boolean),
        sa.column("version", sa.Integer),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        control,
        [{"id": "global", "canary_enabled": True, "version": 1, "updated_at": datetime.now(timezone.utc)}],
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_observations_stable_message_id", table_name="shadow_observations")
    op.drop_index("ix_shadow_observations_created", table_name="shadow_observations")
    op.drop_table("shadow_observations")
    op.drop_index("ix_canary_observations_created", table_name="canary_observations")
    op.drop_table("canary_observations")
    op.drop_table("experiment_controls")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("fallback_used")
