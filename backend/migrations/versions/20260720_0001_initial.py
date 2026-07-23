"""Create the complete Aegis schema.

Revision ID: 20260720_0001
Revises: None
Create Date: 2026-07-20
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("role", sa.Enum("user", "admin", name="userrole"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="ck_users_contact"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("phone"),
    )
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "conversation_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("status", sa.Enum("active", "archived", name="sessionstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_sessions_user_id", "conversation_sessions", ["user_id"])
    op.create_index("ix_sessions_user_updated", "conversation_sessions", ["user_id", "updated_at"])
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.Enum("processing", "ready", "failed", "deleting", "delete_failed", name="documentstatus"), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("uploaded_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_kb_status", "knowledge_documents", ["knowledge_base_id", "status"])
    op.create_index("ix_knowledge_documents_knowledge_base_id", "knowledge_documents", ["knowledge_base_id"])
    op.create_index("ix_knowledge_documents_sha256", "knowledge_documents", ["sha256"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("role", sa.Enum("user", "assistant", name="messagerole"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(40), nullable=True),
        sa.Column("retrieval_status", sa.String(30), nullable=True),
        sa.Column("model_name", sa.String(100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_session_created", "messages", ["session_id", "created_at"])
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    op.create_table(
        "daily_usage",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("question_count >= 0", name="ck_daily_usage_nonnegative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "usage_date", name="uq_daily_usage_user_date"),
    )
    op.create_index("ix_daily_usage_user_id", "daily_usage", ["user_id"])
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("preferred_name", sa.String(80), nullable=True),
        sa.Column("locale", sa.String(20), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("response_style", sa.Enum("concise", "balanced", "detailed", name="responsestyle"), nullable=False),
        sa.Column("memory_consent", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "session_memories",
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("compacted_message_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "conversation_tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("status", sa.Enum("active", "completed", "cancelled", name="taskstatus"), nullable=False),
        sa.Column("slots", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_tasks_session_id", "conversation_tasks", ["session_id"])
    op.create_index("ix_conversation_tasks_user_id", "conversation_tasks", ["user_id"])
    op.create_index("ix_tasks_session_status", "conversation_tasks", ["session_id", "status"])
    op.create_table(
        "handoff_tickets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("active_dedupe_key", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("trigger", sa.Enum("manual", "fallback", "complaint", "negative_feedback", name="handofftrigger"), nullable=False),
        sa.Column("status", sa.Enum("open", "in_progress", "resolved", "closed", name="handoffstatus"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("assigned_to", sa.String(36), nullable=True),
        sa.Column("conversation_summary", sa.Text(), nullable=True),
        sa.Column("resolution", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("priority BETWEEN 1 AND 5", name="ck_handoffs_priority"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["conversation_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_dedupe_key"),
    )
    op.create_index("ix_handoff_tickets_assigned_to", "handoff_tickets", ["assigned_to"])
    op.create_index("ix_handoff_tickets_session_id", "handoff_tickets", ["session_id"])
    op.create_index("ix_handoff_tickets_user_id", "handoff_tickets", ["user_id"])
    op.create_index("ix_handoffs_status_priority_created", "handoff_tickets", ["status", "priority", "created_at"])
    op.create_table(
        "message_citations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=True),
        sa.Column("chunk_id", sa.String(100), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("excerpt", sa.String(500), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_citations_message_id", "message_citations", ["message_id"])
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])
    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "retry", "completed", "dead", name="ingestionjobstatus"), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 0 AND max_attempts BETWEEN 1 AND 20", name="ck_ingestion_job_attempts"),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_jobs_claim", "knowledge_ingestion_jobs", ["status", "available_at", "lease_expires_at"])
    op.create_index(
        "ix_knowledge_ingestion_jobs_document_id", "knowledge_ingestion_jobs", ["document_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("knowledge_ingestion_jobs")
    op.drop_table("feedback")
    op.drop_table("message_citations")
    op.drop_table("handoff_tickets")
    op.drop_table("conversation_tasks")
    op.drop_table("session_memories")
    op.drop_table("user_profiles")
    op.drop_table("daily_usage")
    op.drop_table("messages")
    op.drop_table("knowledge_documents")
    op.drop_table("conversation_sessions")
    op.drop_table("knowledge_bases")
    op.drop_table("users")
