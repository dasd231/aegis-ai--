"""Add privacy-preserving distributed login throttling.

Revision ID: 20260722_0003
Revises: 20260722_0002
Create Date: 2026-07-22
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0003"
down_revision: str | None = "20260722_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_throttles",
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("failures >= 0", name="ck_login_throttles_nonnegative"),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index("ix_login_throttles_updated", "login_throttles", ["updated_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_login_throttles_updated", table_name="login_throttles")
    op.drop_table("login_throttles")
