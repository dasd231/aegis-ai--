from __future__ import annotations

from datetime import datetime
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, model_validator

from app.models import (
    DocumentStatus,
    HandoffStatus,
    HandoffTrigger,
    MessageRole,
    ProfileFactSource,
    ResponseStyle,
    SessionStatus,
    TaskStatus,
    UserRole,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{7,15}$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="新用户", min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_identity_and_password(self):
        if bool(self.email) == bool(self.phone):
            raise ValueError("邮箱和手机号必须且只能填写一项")
        if not any(char.isalpha() for char in self.password) or not any(char.isdigit() for char in self.password):
            raise ValueError("密码必须同时包含字母和数字")
        return self


class LoginRequest(BaseModel):
    account: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class UserOut(ORMModel):
    id: str
    tenant_id: str
    email: str | None
    phone: str | None
    display_name: str
    role: UserRole
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class SessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=120)


class CitationOut(ORMModel):
    id: str
    document_id: str | None
    chunk_id: str
    source_name: str
    excerpt: str
    score: int
    display_order: int


class FeedbackOut(ORMModel):
    id: str
    rating: int
    comment: str | None


class MessageOut(ORMModel):
    id: str
    role: MessageRole
    content: str
    intent: str | None
    retrieval_status: str | None
    model_name: str | None
    experiment_group: str | None
    embedding_pool: str | None
    embedding_version: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    vector_index_version: str | None
    prompt_version: str | None
    prompt_sha256: str | None
    rag_version: str | None
    service_version: str | None
    fallback_used: bool
    request_id: str | None
    ttft_ms: int | None
    output_tokens: int | None
    tool_calls: list
    finish_reason: str | None
    latency_ms: int | None
    created_at: datetime
    citations: list[CitationOut] = []
    feedback: FeedbackOut | None = None


class SessionSummary(ORMModel):
    id: str
    title: str
    status: SessionStatus
    experiment_group: str | None
    model_version: str | None
    embedding_pool: str | None
    embedding_version: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    vector_index_version: str | None
    prompt_version: str | None
    prompt_sha256: str | None
    rag_version: str | None
    service_version: str | None
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionSummary):
    messages: list[MessageOut]


class SessionPage(BaseModel):
    items: list[SessionSummary]
    total: int
    page: int
    page_size: int


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class KnowledgeBaseOut(ORMModel):
    id: str
    name: str
    description: str
    is_active: bool
    created_at: datetime


class DocumentOut(ORMModel):
    id: str
    knowledge_base_id: str
    original_name: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int
    page: int
    page_size: int


class ChatRequest(BaseModel):
    session_id: str
    question: str = Field(min_length=1)
    knowledge_base_id: str | None = None

    @model_validator(mode="after")
    def normalize_question(self):
        self.question = self.question.strip()
        if not self.question:
            raise ValueError("问题不能为空")
        return self


class FeedbackRequest(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_rating(self):
        if self.rating not in {-1, 1}:
            raise ValueError("评价值只能为赞同或不赞同")
        if self.comment:
            self.comment = self.comment.strip() or None
        return self


class UsageOut(BaseModel):
    used: int
    limit: int
    remaining: int


class AdminOverview(BaseModel):
    users: int
    sessions: int
    questions_today: int
    positive_feedback: int
    negative_feedback: int
    daily_questions: list[dict]


class ExperimentControlOut(ORMModel):
    id: str
    canary_enabled: bool
    disabled_reason: str | None
    disabled_at: datetime | None
    stage: int
    model_canary_percent: int
    embedding_canary_percent: int
    prompt_canary_percent: int
    rag_canary_percent: int
    service_canary_percent: int
    shadow_canary_percent: int
    version: int
    updated_at: datetime


class ExperimentControlUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    canary_enabled: bool
    stage: int | None = Field(default=None, ge=0, le=7)
    model_canary_percent: int | None = Field(default=None, ge=0, le=100)
    embedding_canary_percent: int | None = Field(default=None, ge=0, le=100)
    prompt_canary_percent: int | None = Field(default=None, ge=0, le=100)
    rag_canary_percent: int | None = Field(default=None, ge=0, le=100)
    service_canary_percent: int | None = Field(default=None, ge=0, le=100)
    shadow_canary_percent: int | None = Field(default=None, ge=0, le=100)
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def normalize_reason(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("变更原因不能为空")
        return self


class ExperimentControlAuditOut(ORMModel):
    id: str
    admin_id: str | None
    action: str
    from_version: int
    to_version: int
    previous_config: dict
    next_config: dict
    reason: str
    created_at: datetime


class AdminDataAccessAuditOut(ORMModel):
    id: str
    admin_id: str | None
    action: str
    target_type: str
    target_id: str | None
    request_id: str | None
    purpose: str
    created_at: datetime


class ProviderCredentialOut(ORMModel):
    id: str
    display_name: str
    category: str
    provider: str
    base_url: str
    model: str
    enabled: bool
    has_api_key: bool
    api_key_fingerprint: str
    validation_status: str
    last_error_code: str | None
    last_validated_at: datetime | None
    version: int
    updated_at: datetime


class ProviderCredentialUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    provider: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_./:-]+$")
    api_key: SecretStr | None = None
    enabled: bool
    validate_before_save: bool = False
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def normalize_provider_update(self):
        self.provider = self.provider.strip().lower()
        self.base_url = self.base_url.strip().rstrip("/")
        self.model = self.model.strip()
        self.reason = self.reason.strip()
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            self.api_key = None
        return self


class ProviderValidationOut(BaseModel):
    ok: bool
    status: str
    error_code: str | None = None
    latency_ms: int
    model: str


class ProviderCredentialAuditOut(ORMModel):
    id: str
    credential_id: str
    admin_id: str | None
    request_id: str | None
    action: str
    from_version: int | None
    to_version: int | None
    changed_fields: list[str]
    succeeded: bool
    error_code: str | None
    created_at: datetime


class AdminSessionOut(BaseModel):
    id: str
    user_id: str
    user_display_name: str
    title: str
    status: SessionStatus
    message_count: int
    created_at: datetime
    updated_at: datetime


class AdminSessionPage(BaseModel):
    items: list[AdminSessionOut]
    total: int
    page: int
    page_size: int


class AdminSessionDetail(AdminSessionOut):
    messages: list[MessageOut]


class UserProfileOut(ORMModel):
    user_id: str
    preferred_name: str | None
    locale: str
    timezone: str
    response_style: ResponseStyle
    memory_consent: bool
    consent_version: str | None
    profile_learning_consent: bool
    profile_consent_version: str | None
    facts: list["UserProfileFactOut"] = Field(default_factory=list)
    service_summary: "UserServiceSummary | None" = None
    created_at: datetime
    updated_at: datetime


class UserProfileUpdate(BaseModel):
    preferred_name: str | None = Field(default=None, max_length=80)
    locale: str = Field(default="zh-CN", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
    timezone: str = Field(default="Asia/Hong_Kong", max_length=64)
    response_style: ResponseStyle = ResponseStyle.BALANCED
    memory_consent: bool = False
    consent_version: str | None = Field(default=None, max_length=40)
    profile_learning_consent: bool = False
    profile_consent_version: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def normalize_and_validate(self):
        self.preferred_name = self.preferred_name.strip() if self.preferred_name else None
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("无法识别该标准时区") from exc
        if self.memory_consent and not self.consent_version:
            raise ValueError("启用长期记忆前必须确认隐私授权版本")
        if not self.memory_consent:
            self.consent_version = None
        if self.profile_learning_consent and not self.profile_consent_version:
            raise ValueError("启用画像学习前必须确认画像授权版本")
        if not self.profile_learning_consent:
            self.profile_consent_version = None
        return self


class UserProfileFactOut(ORMModel):
    id: str
    fact_type: str
    value: str
    source: ProfileFactSource
    confidence: int
    source_message_id: str | None
    expires_at: datetime | None
    last_confirmed_at: datetime
    created_at: datetime


class UserProfileFactCreate(BaseModel):
    fact_type: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^(device_model|purchase_channel|preferred_contact_channel|service_region)$",
    )
    value: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def normalize(self):
        self.value = " ".join(self.value.split())
        return self


class UserServiceSummary(BaseModel):
    total_questions: int = 0
    positive_feedback: int = 0
    negative_feedback: int = 0
    handoff_count: int = 0
    active_task_count: int = 0
    top_intents: list[str] = Field(default_factory=list)
    last_interaction_at: datetime | None = None


TaskSlotValue = str | int | float | bool | None


def validate_task_slots(slots: dict[str, TaskSlotValue]) -> dict[str, TaskSlotValue]:
    if len(slots) > 30 or len(json.dumps(slots, ensure_ascii=False)) > 4000:
        raise ValueError("任务参数数量或内容长度超过限制")
    if any(len(key) > 80 for key in slots):
        raise ValueError("任务参数名称过长")
    return slots


class TaskCreate(BaseModel):
    task_type: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    slots: dict[str, TaskSlotValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self):
        self.slots = validate_task_slots(self.slots)
        return self


class TaskUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    status: TaskStatus | None = None
    slots: dict[str, TaskSlotValue] | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.status is None and self.slots is None:
            raise ValueError("必须提供任务状态或任务参数")
        if self.slots is not None:
            self.slots = validate_task_slots(self.slots)
        return self


class TaskOut(ORMModel):
    id: str
    session_id: str
    user_id: str
    task_type: str
    status: TaskStatus
    slots: dict[str, TaskSlotValue]
    version: int
    created_at: datetime
    updated_at: datetime


class HandoffCreate(BaseModel):
    session_id: str
    trigger: HandoffTrigger = HandoffTrigger.MANUAL
    reason: str = Field(min_length=1, max_length=500)
    priority: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def normalize_reason(self):
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("转人工原因不能为空")
        return self


class HandoffAdminUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    status: HandoffStatus | None = None
    assigned_to: str | None = None
    resolution: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_change(self):
        if self.status is None and self.assigned_to is None and self.resolution is None:
            raise ValueError("至少需要提供一个待更新字段")
        if self.resolution:
            self.resolution = self.resolution.strip() or None
        return self


class HandoffOut(ORMModel):
    id: str
    session_id: str
    user_id: str
    trigger: HandoffTrigger
    status: HandoffStatus
    reason: str
    priority: int
    assigned_to: str | None
    conversation_summary: str | None
    resolution: str | None
    version: int
    created_at: datetime
    updated_at: datetime
