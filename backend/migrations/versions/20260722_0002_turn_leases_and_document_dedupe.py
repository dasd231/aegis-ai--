"""Add distributed turn leases and document content deduplication.

Revision ID: 20260722_0002
Revises: 20260720_0001
Create Date: 2026-07-22
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0002"
down_revision: str | None = "20260720_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT knowledge_base_id, sha256 FROM knowledge_documents "
            "GROUP BY knowledge_base_id, sha256 HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot add uq_documents_kb_sha256: duplicate document content exists; "
            "resolve duplicate metadata before retrying the migration"
        )
    with op.batch_alter_table("conversation_sessions") as batch_op:
        batch_op.add_column(sa.Column("turn_lock_token", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("turn_lock_expires_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.create_unique_constraint(
            "uq_documents_kb_sha256",
            ["knowledge_base_id", "sha256"],
        )


def downgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_constraint("uq_documents_kb_sha256", type_="unique")
    with op.batch_alter_table("conversation_sessions") as batch_op:
        batch_op.drop_column("turn_lock_expires_at")
        batch_op.drop_column("turn_lock_token")
