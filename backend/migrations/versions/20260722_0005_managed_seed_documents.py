"""Track managed seed documents for safe dataset replacement.

Revision ID: 20260722_0005
Revises: 20260722_0004
Create Date: 2026-07-22
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0005"
down_revision: str | None = "20260722_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.add_column(
            sa.Column("is_seed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute(
        sa.text(
            """
            UPDATE knowledge_documents
            SET is_seed = true
            WHERE original_name IN (
                '公司产品介绍.txt', '常见问题FAQ.md', '退换货与退款政策.txt'
            )
              AND knowledge_base_id IN (
                SELECT id FROM knowledge_bases WHERE name = '示例企业知识库'
              )
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("knowledge_documents") as batch_op:
        batch_op.drop_column("is_seed")
