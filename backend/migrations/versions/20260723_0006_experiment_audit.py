"""Persist conversation-stable experiment assignments and request versions.

Revision ID: 20260723_0006
Revises: 20260722_0005
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0006"
down_revision: str | None = "20260722_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    session_columns = {
        "experiment_group": sa.Column("experiment_group", sa.String(length=20), nullable=True),
        "model_pool": sa.Column("model_pool", sa.String(length=20), nullable=True),
        "model_version": sa.Column("model_version", sa.String(length=100), nullable=True),
        "prompt_version": sa.Column("prompt_version", sa.String(length=40), nullable=True),
        "prompt_sha256": sa.Column("prompt_sha256", sa.String(length=64), nullable=True),
        "rag_version": sa.Column("rag_version", sa.String(length=40), nullable=True),
        "service_pool": sa.Column("service_pool", sa.String(length=20), nullable=True),
        "service_version": sa.Column("service_version", sa.String(length=80), nullable=True),
        "experiment_buckets": sa.Column("experiment_buckets", sa.JSON(), nullable=True),
    }
    existing_session_columns = _column_names("conversation_sessions")
    with op.batch_alter_table("conversation_sessions") as batch_op:
        for name, column in session_columns.items():
            if name not in existing_session_columns:
                batch_op.add_column(column)

    message_columns = {
        "experiment_group": sa.Column("experiment_group", sa.String(length=20), nullable=True),
        "prompt_version": sa.Column("prompt_version", sa.String(length=40), nullable=True),
        "prompt_sha256": sa.Column("prompt_sha256", sa.String(length=64), nullable=True),
        "rag_version": sa.Column("rag_version", sa.String(length=40), nullable=True),
        "service_version": sa.Column("service_version", sa.String(length=80), nullable=True),
        "request_id": sa.Column("request_id", sa.String(length=64), nullable=True),
        "ttft_ms": sa.Column("ttft_ms", sa.Integer(), nullable=True),
        "output_tokens": sa.Column("output_tokens", sa.Integer(), nullable=True),
        # MySQL rejects a quoted default on JSON even though SQLite accepts it.
        # Add nullable, backfill through SQLAlchemy's JSON serializer, then
        # enforce NOT NULL below so upgrades with existing messages are safe.
        "tool_calls": sa.Column("tool_calls", sa.JSON(), nullable=True),
        "finish_reason": sa.Column("finish_reason", sa.String(length=40), nullable=True),
    }
    existing_message_columns = _column_names("messages")
    with op.batch_alter_table("messages") as batch_op:
        for name, column in message_columns.items():
            if name not in existing_message_columns:
                batch_op.add_column(column)

    messages = sa.table("messages", sa.column("tool_calls", sa.JSON()))
    op.execute(messages.update().where(messages.c.tool_calls.is_(None)).values(tool_calls=[]))
    with op.batch_alter_table("messages") as batch_op:
        batch_op.alter_column("tool_calls", existing_type=sa.JSON(), nullable=False)

    if "ix_messages_request_id" not in _index_names("messages"):
        if "ix_messages_request_id" in _index_names("conversation_sessions"):
            with op.batch_alter_table("conversation_sessions") as batch_op:
                batch_op.drop_index("ix_messages_request_id")
        op.create_index("ix_messages_request_id", "messages", ["request_id"])


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_index("ix_messages_request_id")
        batch_op.drop_column("finish_reason")
        batch_op.drop_column("tool_calls")
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("ttft_ms")
        batch_op.drop_column("request_id")
        batch_op.drop_column("service_version")
        batch_op.drop_column("rag_version")
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("prompt_sha256")
        batch_op.drop_column("experiment_group")
    with op.batch_alter_table("conversation_sessions") as batch_op:
        batch_op.drop_column("experiment_buckets")
        batch_op.drop_column("service_version")
        batch_op.drop_column("service_pool")
        batch_op.drop_column("rag_version")
        batch_op.drop_column("prompt_version")
        batch_op.drop_column("prompt_sha256")
        batch_op.drop_column("model_version")
        batch_op.drop_column("model_pool")
        batch_op.drop_column("experiment_group")
