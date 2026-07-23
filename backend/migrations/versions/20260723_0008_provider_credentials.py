"""Add encrypted provider credential registry and immutable audit metadata.

Revision ID: 20260723_0008
Revises: 20260723_0007
Create Date: 2026-07-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260723_0008"
down_revision: str | None = "20260723_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("api_key_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_provider_credentials_version"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_credentials_enabled", "provider_credentials", ["enabled"])
    op.create_table(
        "provider_credential_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("credential_id", sa.String(length=40), nullable=False),
        sa.Column("admin_id", sa.String(length=36), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=True),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_audits_credential_created",
        "provider_credential_audits",
        ["credential_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_audits_credential_created", table_name="provider_credential_audits")
    op.drop_table("provider_credential_audits")
    op.drop_index("ix_provider_credentials_enabled", table_name="provider_credentials")
    op.drop_table("provider_credentials")
