"""Add optimistic versioning to human handoff tickets.

Revision ID: 20260722_0004
Revises: 20260722_0003
Create Date: 2026-07-22
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0004"
down_revision: str | None = "20260722_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("handoff_tickets") as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
        batch_op.create_check_constraint("ck_handoffs_version", "version >= 1")


def downgrade() -> None:
    with op.batch_alter_table("handoff_tickets") as batch_op:
        batch_op.drop_constraint("ck_handoffs_version", type_="check")
        batch_op.drop_column("version")
