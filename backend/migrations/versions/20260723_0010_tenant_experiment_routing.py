"""Add tenant identity used by conversation-stable experiment routing.

Revision ID: 20260723_0010
Revises: 20260723_0009
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0010"
down_revision: str | None = "20260723_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            )
        )
        batch_op.create_index("ix_users_tenant_id", ["tenant_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_tenant_id")
        batch_op.drop_column("tenant_id")
