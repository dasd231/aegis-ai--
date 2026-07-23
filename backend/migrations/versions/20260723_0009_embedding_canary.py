"""Bind stable/canary embedding models to independent vector index generations.

Revision ID: 20260723_0009
Revises: 20260723_0008
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0009"
down_revision: str | None = "20260723_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Revision 0006 briefly existed with request telemetry on the session table.
    # Repair already-upgraded development databases while fresh installs use the
    # corrected migration. The checks make this migration safe in both states.
    message_columns = _column_names("messages")
    missing_message_columns = {
        "request_id": sa.Column("request_id", sa.String(length=64), nullable=True),
        "ttft_ms": sa.Column("ttft_ms", sa.Integer(), nullable=True),
        "output_tokens": sa.Column("output_tokens", sa.Integer(), nullable=True),
        "tool_calls": sa.Column("tool_calls", sa.JSON(), nullable=True),
        "finish_reason": sa.Column("finish_reason", sa.String(length=40), nullable=True),
    }
    with op.batch_alter_table("messages") as batch_op:
        for name, column in missing_message_columns.items():
            if name not in message_columns:
                batch_op.add_column(column)
    messages = sa.table("messages", sa.column("tool_calls", sa.JSON()))
    op.execute(messages.update().where(messages.c.tool_calls.is_(None)).values(tool_calls=[]))
    with op.batch_alter_table("messages") as batch_op:
        batch_op.alter_column("tool_calls", existing_type=sa.JSON(), nullable=False)
    message_indexes = {
        str(item["name"]) for item in sa.inspect(op.get_bind()).get_indexes("messages")
    }
    if "ix_messages_request_id" not in message_indexes:
        # SQLite index names are database-global. Remove the misplaced legacy
        # index before creating the correctly scoped one.
        session_indexes = {
            str(item["name"])
            for item in sa.inspect(op.get_bind()).get_indexes("conversation_sessions")
        }
        if "ix_messages_request_id" in session_indexes:
            with op.batch_alter_table("conversation_sessions") as batch_op:
                batch_op.drop_index("ix_messages_request_id")
        op.create_index("ix_messages_request_id", "messages", ["request_id"])

    with op.batch_alter_table("experiment_controls") as batch_op:
        batch_op.add_column(sa.Column("stage", sa.Integer(), nullable=False, server_default="0"))
        for name in (
            "model_canary_percent",
            "embedding_canary_percent",
            "prompt_canary_percent",
            "rag_canary_percent",
            "service_canary_percent",
            "shadow_canary_percent",
        ):
            batch_op.add_column(
                sa.Column(name, sa.Integer(), nullable=False, server_default="0")
            )
    op.create_table(
        "experiment_control_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("admin_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("previous_config", sa.JSON(), nullable=False),
        sa.Column("next_config", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_experiment_audits_created",
        "experiment_control_audits",
        ["created_at"],
    )

    session_fields = (
        sa.Column("embedding_pool", sa.String(length=20), nullable=True),
        sa.Column("embedding_version", sa.String(length=80), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("vector_index_version", sa.String(length=120), nullable=True),
    )
    message_fields = tuple(column.copy() for column in session_fields)
    with op.batch_alter_table("conversation_sessions") as batch_op:
        for column in session_fields:
            batch_op.add_column(column)
    with op.batch_alter_table("messages") as batch_op:
        for column in message_fields:
            batch_op.add_column(column)

    for table in ("canary_observations", "shadow_observations"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "embedding_version",
                    sa.String(length=80),
                    nullable=False,
                    server_default="legacy-stable",
                )
            )
            batch_op.add_column(
                sa.Column(
                    "embedding_model",
                    sa.String(length=100),
                    nullable=False,
                    server_default="unknown",
                )
            )
            batch_op.add_column(
                sa.Column(
                    "vector_index_version",
                    sa.String(length=120),
                    nullable=False,
                    server_default="legacy",
                )
            )


def downgrade() -> None:
    op.drop_index("ix_experiment_audits_created", table_name="experiment_control_audits")
    op.drop_table("experiment_control_audits")
    with op.batch_alter_table("experiment_controls") as batch_op:
        batch_op.drop_column("shadow_canary_percent")
        batch_op.drop_column("service_canary_percent")
        batch_op.drop_column("rag_canary_percent")
        batch_op.drop_column("prompt_canary_percent")
        batch_op.drop_column("embedding_canary_percent")
        batch_op.drop_column("model_canary_percent")
        batch_op.drop_column("stage")
    for table in ("shadow_observations", "canary_observations"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("vector_index_version")
            batch_op.drop_column("embedding_model")
            batch_op.drop_column("embedding_version")
    for table in ("messages", "conversation_sessions"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("vector_index_version")
            batch_op.drop_column("embedding_dimensions")
            batch_op.drop_column("embedding_model")
            batch_op.drop_column("embedding_version")
            batch_op.drop_column("embedding_pool")
