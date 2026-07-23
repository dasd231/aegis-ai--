"""Add consented, attributable user profile facts.

Revision ID: 20260723_0012
Revises: 20260723_0011
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0012"
down_revision: str | None = "20260723_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "profile_learning_consent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("profile_consent_version", sa.String(length=40), nullable=True)
        )

    op.create_table(
        "user_profile_facts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("fact_type", sa.String(length=40), nullable=False),
        sa.Column("value", sa.String(length=160), nullable=False),
        sa.Column("normalized_value", sa.String(length=160), nullable=False),
        sa.Column(
            "source",
            sa.Enum("explicit", "conversation", "crm", name="profilefactsource"),
            nullable=False,
        ),
        sa.Column("source_message_id", sa.String(length=36), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence BETWEEN 0 AND 10000", name="ck_profile_fact_confidence"),
        sa.ForeignKeyConstraint(["source_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "fact_type", "normalized_value", name="uq_profile_fact_identity"
        ),
    )
    op.create_index("ix_user_profile_facts_user_id", "user_profile_facts", ["user_id"])
    op.create_index(
        "ix_user_profile_facts_source_message_id",
        "user_profile_facts",
        ["source_message_id"],
    )
    op.create_index(
        "ix_profile_facts_user_active",
        "user_profile_facts",
        ["user_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_profile_facts_user_active", table_name="user_profile_facts")
    op.drop_index("ix_user_profile_facts_source_message_id", table_name="user_profile_facts")
    op.drop_index("ix_user_profile_facts_user_id", table_name="user_profile_facts")
    op.drop_table("user_profile_facts")
    with op.batch_alter_table("user_profiles") as batch_op:
        batch_op.drop_column("profile_consent_version")
        batch_op.drop_column("profile_learning_consent")
