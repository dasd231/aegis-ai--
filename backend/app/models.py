from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def uuid4_str() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(item.value) for item in enum_class]


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class DocumentStatus(str, enum.Enum):
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETE_FAILED = "delete_failed"


class ResponseStyle(str, enum.Enum):
    CONCISE = "concise"
    BALANCED = "balanced"
    DETAILED = "detailed"


class ProfileFactSource(str, enum.Enum):
    EXPLICIT = "explicit"
    CONVERSATION = "conversation"
    CRM = "crm"


class TaskStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class HandoffStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class HandoffTrigger(str, enum.Enum):
    MANUAL = "manual"
    FALLBACK = "fallback"
    COMPLAINT = "complaint"
    NEGATIVE_FEEDBACK = "negative_feedback"


class IngestionJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    COMPLETED = "completed"
    DEAD = "dead"


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    __table_args__ = (
        CheckConstraint("failures >= 0", name="ck_login_throttles_nonnegative"),
        Index("ix_login_throttles_updated", "updated_at"),
    )

    # HMAC(account), never the raw email/phone. It is intentionally not tied
    # to users so enumeration attempts for nonexistent accounts are throttled too.
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="ck_users_contact"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80), default="新用户")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, values_callable=enum_values), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sessions: Mapped[list[ConversationSession]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"
    __table_args__ = (Index("ix_sessions_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, values_callable=enum_values), default=SessionStatus.ACTIVE
    )
    turn_lock_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    experiment_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model_pool: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_pool: Mapped[str | None] = mapped_column(String(20), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_index_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rag_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    service_pool: Mapped[str | None] = mapped_column(String(20), nullable=True)
    service_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    experiment_buckets: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")
    messages: Mapped[list[Message]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        Index("ix_messages_request_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, values_callable=enum_values))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    retrieval_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    experiment_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    embedding_pool: Mapped[str | None] = mapped_column(String(20), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_index_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rag_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    service_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    finish_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[ConversationSession] = relationship(back_populates="messages")
    citations: Mapped[list[MessageCitation]] = relationship(back_populates="message", cascade="all, delete-orphan")
    feedback: Mapped[Feedback | None] = relationship(back_populates="message", cascade="all, delete-orphan")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    documents: Mapped[list[KnowledgeDocument]] = relationship(back_populates="knowledge_base")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index("ix_documents_kb_status", "knowledge_base_id", "status"),
        UniqueConstraint("knowledge_base_id", "sha256", name="uq_documents_kb_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, values_callable=enum_values), default=DocumentStatus.PROCESSING
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")


class MessageCitation(Base):
    __tablename__ = "message_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[str] = mapped_column(String(100))
    source_name: Mapped[str] = mapped_column(String(255))
    excerpt: Mapped[str] = mapped_column(String(500))
    score: Mapped[int] = mapped_column(Integer)
    display_order: Mapped[int] = mapped_column(Integer)

    message: Mapped[Message] = relationship(back_populates="citations")


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (CheckConstraint("rating IN (-1, 1)", name="ck_feedback_rating"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    message: Mapped[Message] = relationship(back_populates="feedback")


class DailyUsage(Base):
    __tablename__ = "daily_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_daily_usage_user_date"),
        CheckConstraint("question_count >= 0", name="ck_daily_usage_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    usage_date: Mapped[date] = mapped_column(Date)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    preferred_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locale: Mapped[str] = mapped_column(String(20), default="zh-CN")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Hong_Kong")
    response_style: Mapped[ResponseStyle] = mapped_column(
        Enum(ResponseStyle, values_callable=enum_values), default=ResponseStyle.BALANCED
    )
    memory_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profile_learning_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_consent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserProfileFact(Base):
    __tablename__ = "user_profile_facts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "fact_type", "normalized_value", name="uq_profile_fact_identity"
        ),
        CheckConstraint("confidence BETWEEN 0 AND 10000", name="ck_profile_fact_confidence"),
        Index("ix_profile_facts_user_active", "user_id", "revoked_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    fact_type: Mapped[str] = mapped_column(String(40))
    value: Mapped[str] = mapped_column(String(160))
    normalized_value: Mapped[str] = mapped_column(String(160))
    source: Mapped[ProfileFactSource] = mapped_column(
        Enum(ProfileFactSource, values_callable=enum_values)
    )
    source_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence: Mapped[int] = mapped_column(Integer, default=10000)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SessionMemory(Base):
    __tablename__ = "session_memories"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    compacted_message_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConversationTask(Base):
    __tablename__ = "conversation_tasks"
    __table_args__ = (Index("ix_tasks_session_status", "session_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, values_callable=enum_values), default=TaskStatus.ACTIVE
    )
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class HandoffTicket(Base):
    __tablename__ = "handoff_tickets"
    __table_args__ = (
        Index("ix_handoffs_status_priority_created", "status", "priority", "created_at"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="ck_handoffs_priority"),
        CheckConstraint("version >= 1", name="ck_handoffs_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id", ondelete="CASCADE"), index=True)
    # MySQL has no partial unique index. This nullable key is session_id only
    # while the ticket is active, so concurrent creates are DB-enforced.
    active_dedupe_key: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[HandoffTrigger] = mapped_column(Enum(HandoffTrigger, values_callable=enum_values))
    status: Mapped[HandoffStatus] = mapped_column(
        Enum(HandoffStatus, values_callable=enum_values), default=HandoffStatus.OPEN
    )
    reason: Mapped[str] = mapped_column(String(500))
    priority: Mapped[int] = mapped_column(Integer, default=3)
    assigned_to: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExperimentControl(Base):
    __tablename__ = "experiment_controls"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default="global")
    canary_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    model_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    embedding_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    prompt_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    rag_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    service_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    shadow_canary_percent: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExperimentControlAudit(Base):
    __tablename__ = "experiment_control_audits"
    __table_args__ = (Index("ix_experiment_audits_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(30))
    from_version: Mapped[int] = mapped_column(Integer)
    to_version: Mapped[int] = mapped_column(Integer)
    previous_config: Mapped[dict] = mapped_column(JSON)
    next_config: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminDataAccessAudit(Base):
    """Append-only application audit for privileged customer-data reads."""

    __tablename__ = "admin_data_access_audits"
    __table_args__ = (Index("ix_admin_data_access_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(40))
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanaryObservation(Base):
    __tablename__ = "canary_observations"
    __table_args__ = (Index("ix_canary_observations_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    experiment_group: Mapped[str] = mapped_column(String(20))
    model_version: Mapped[str] = mapped_column(String(100))
    embedding_version: Mapped[str] = mapped_column(String(80))
    embedding_model: Mapped[str] = mapped_column(String(100))
    vector_index_version: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    rag_version: Mapped[str] = mapped_column(String(40))
    service_version: Mapped[str] = mapped_column(String(80))
    success: Mapped[bool] = mapped_column(Boolean)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShadowObservation(Base):
    __tablename__ = "shadow_observations"
    __table_args__ = (Index("ix_shadow_observations_created", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    stable_message_id: Mapped[str] = mapped_column(String(36), index=True)
    model_version: Mapped[str] = mapped_column(String(100))
    embedding_version: Mapped[str] = mapped_column(String(80))
    embedding_model: Mapped[str] = mapped_column(String(100))
    vector_index_version: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    rag_version: Mapped[str] = mapped_column(String(40))
    service_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))
    latency_ms: Mapped[int] = mapped_column(Integer)
    stable_answer_sha256: Mapped[str] = mapped_column(String(64))
    canary_answer_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canary_answer_chars: Mapped[int] = mapped_column(Integer, default=0)
    lexical_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    side_effects_mocked: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderCredential(Base):
    """Encrypted provider secret plus non-secret runtime configuration.

    The API never serializes ``api_key_ciphertext``. ``api_key_fingerprint`` is
    a keyed digest used only to tell operators whether two deployments use the
    same credential without exposing any prefix or suffix of the secret.
    """

    __tablename__ = "provider_credentials"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_provider_credentials_version"),
        Index("ix_provider_credentials_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    category: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(100))
    api_key_ciphertext: Mapped[str] = mapped_column(Text)
    api_key_fingerprint: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_status: Mapped[str] = mapped_column(String(20), default="unverified")
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProviderCredentialAudit(Base):
    __tablename__ = "provider_credential_audits"
    __table_args__ = (Index("ix_provider_audits_credential_created", "credential_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    credential_id: Mapped[str] = mapped_column(String(40))
    admin_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(30))
    from_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_fields: Mapped[list] = mapped_column(JSON, default=list)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeIngestionJob(Base):
    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        Index("ix_ingestion_jobs_claim", "status", "available_at", "lease_expires_at"),
        CheckConstraint("attempts >= 0 AND max_attempts BETWEEN 1 AND 20", name="ck_ingestion_job_attempts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[IngestionJobStatus] = mapped_column(
        Enum(IngestionJobStatus, values_callable=enum_values), default=IngestionJobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
