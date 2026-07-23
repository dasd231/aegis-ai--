from __future__ import annotations

from functools import lru_cache
import hashlib
import hmac
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Aegis AI 客服"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    secret_key: SecretStr = SecretStr("development-only-change-me-at-least-32-chars")
    access_token_minutes: int = Field(default=120, ge=5, le=10080)
    jwt_issuer: str = "aegis-api"
    jwt_audience: str = "aegis-web"
    login_max_failures: int = Field(default=5, ge=2, le=50)
    login_failure_window_seconds: int = Field(default=900, ge=60, le=86400)
    login_lock_seconds: int = Field(default=900, ge=60, le=86400)
    allowed_origins: str = "http://localhost:5173,http://localhost:8080"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    business_timezone: str = "Asia/Hong_Kong"
    max_inflight_requests: int = Field(default=200, ge=10, le=10000)
    request_queue_timeout_seconds: float = Field(default=2.0, ge=0.05, le=30)

    database_url: str = "sqlite:///./data/aegis.db"
    auto_create_tables: bool = True
    auto_seed: bool = True
    ingestion_mode: Literal["inline", "queue"] = "inline"
    ingestion_poll_seconds: float = Field(default=1.0, ge=0.1, le=30)
    ingestion_lease_seconds: int = Field(default=300, ge=30, le=3600)
    initial_admin_email: str = "admin@example.com"
    initial_admin_password: SecretStr = SecretStr("Admin123!")

    upload_dir: Path = BACKEND_DIR / "uploads"
    qdrant_path: Path = BACKEND_DIR / "qdrant_data"
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_collection: str = Field(default="aegis_knowledge_chunks", pattern=r"^[A-Za-z0-9_-]{1,120}$")
    qdrant_timeout_seconds: float = Field(default=10, ge=1, le=60)
    qdrant_prefer_grpc: bool = False
    max_upload_mb: int = Field(default=15, ge=1, le=50)
    allowed_extensions: str = ".txt,.md,.pdf,.png,.jpg,.jpeg,.webp"
    max_image_pixels: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    max_pdf_vision_pages: int = Field(default=10, ge=0, le=50)

    llm_provider: Literal["mock", "openai_compatible"] = "mock"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=45, ge=5, le=180)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_output_tokens: int = Field(default=800, ge=100, le=4000)
    llm_max_concurrency: int = Field(default=32, ge=1, le=1000)
    llm_queue_timeout_seconds: float = Field(default=5, ge=0.1, le=60)

    # Provider credentials saved from the administrator console are encrypted
    # before reaching the database. A dedicated key is preferred; when empty,
    # the application secret is domain-separated for local/demo deployments.
    credential_encryption_key: SecretStr = SecretStr("")
    provider_allowed_hosts: str = (
        "api.deepseek.com,api.openai.com,openrouter.ai,dashscope.aliyuncs.com,"
        "dashscope-intl.aliyuncs.com,dashscope-us.aliyuncs.com,.maas.aliyuncs.com,"
        "localhost,127.0.0.1,host.docker.internal,vllm-stable,vllm-canary,ollama"
    )
    dashscope_api_key: SecretStr = SecretStr("")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # Explicit bootstrap trust is intended for an operator-validated secret in
    # an ignored local environment. Normal UI updates always validate remotely.
    dashscope_bootstrap_validated: bool = False

    # Conversation-stable experiment routing. Every percentage is independent,
    # while each axis remains fixed for the lifetime of a conversation.
    experiment_salt: SecretStr = SecretStr("development-experiment-salt")
    experiment_whitelist_tenant_ids: str = ""
    experiment_whitelist_user_ids: str = ""
    experiment_whitelist_conversation_ids: str = ""
    experiment_blacklist_tenant_ids: str = ""
    experiment_blacklist_user_ids: str = ""
    experiment_blacklist_conversation_ids: str = ""
    experiment_stable_intents: str = "投诉,售后问题"
    model_canary_percent: int = Field(default=0, ge=0, le=100)
    embedding_canary_percent: int = Field(default=0, ge=0, le=100)
    prompt_canary_percent: int = Field(default=0, ge=0, le=100)
    rag_canary_percent: int = Field(default=0, ge=0, le=100)
    service_canary_percent: int = Field(default=0, ge=0, le=100)
    shadow_canary_percent: int = Field(default=0, ge=0, le=100)
    shadow_timeout_seconds: int = Field(default=60, ge=5, le=300)
    canary_rollback_window_seconds: int = Field(default=300, ge=60, le=3600)
    canary_rollback_min_samples: int = Field(default=20, ge=5, le=10000)
    canary_rollback_error_rate: float = Field(default=0.10, ge=0.001, le=1)
    canary_rollback_timeout_rate: float = Field(default=0.05, ge=0.001, le=1)
    canary_rollback_tool_failure_rate: float = Field(default=0.05, ge=0.001, le=1)
    canary_rollback_ttft_p95_ms: int = Field(default=4000, ge=100, le=120000)
    prompt_stable_version: str = Field(default="customer_v1", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    prompt_canary_version: str = Field(default="customer_v2", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    rag_stable_version: str = Field(default="hybrid_rrf_v2", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    rag_canary_version: str = Field(default="multi_rrf_v3", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    llm_service_stable_version: str = Field(default="vllm-stable", pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    llm_service_canary_version: str = Field(default="vllm-canary", pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    llm_canary_base_url: str = ""
    llm_canary_api_key: SecretStr = SecretStr("")
    llm_canary_model: str = ""

    multimodal_provider: Literal["disabled", "openai_compatible"] = "disabled"
    vision_model: str = "gpt-4o-mini"
    vision_max_output_tokens: int = Field(default=1200, ge=100, le=4000)

    embedding_provider: Literal["local_hash", "openai"] = "local_hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=384, ge=64, le=3072)
    embedding_send_dimensions: bool = True
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_timeout_seconds: float = Field(default=20, ge=1, le=120)
    embedding_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_batch_size: int = Field(default=64, ge=1, le=256)
    embedding_max_concurrency: int = Field(default=64, ge=1, le=1000)
    embedding_queue_timeout_seconds: float = Field(default=2, ge=0.1, le=30)
    embedding_query_cache_size: int = Field(default=512, ge=0, le=10000)
    embedding_query_cache_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    # The canary embedding model and its physical index are one atomic unit.
    # Different dimensions are supported because queries never cross pools.
    embedding_stable_version: str = Field(
        default="embedding-stable-v1", pattern=r"^[A-Za-z0-9_.-]{1,80}$"
    )
    embedding_canary_version: str = Field(
        default="embedding-canary-v1", pattern=r"^[A-Za-z0-9_.-]{1,80}$"
    )
    embedding_canary_model: str = ""
    embedding_canary_dimensions: int = Field(default=512, ge=64, le=3072)
    embedding_canary_send_dimensions: bool = True
    embedding_canary_base_url: str = ""
    embedding_canary_api_key: SecretStr = SecretStr("")
    embedding_canary_batch_size: int = Field(default=10, ge=1, le=256)
    qdrant_canary_collection: str = Field(
        default="", pattern=r"^$|^[A-Za-z0-9_-]{1,120}$"
    )

    retrieval_top_k: int = Field(default=12, ge=3, le=50)
    retrieval_final_k: int = Field(default=6, ge=1, le=12)
    retrieval_max_distance: float = Field(default=0.68, ge=0, le=2)
    retrieval_min_lexical_score: float = Field(default=0.08, ge=0, le=1)
    retrieval_critical_margin: float = Field(default=0.10, ge=0, le=0.5)
    retrieval_rrf_k: int = Field(default=60, ge=1, le=200)
    retrieval_dense_weight: float = Field(default=1.0, ge=0.1, le=5)
    retrieval_sparse_weight: float = Field(default=1.0, ge=0.1, le=5)
    retrieval_expansion_weight: float = Field(default=0.75, ge=0.1, le=2)
    rag_canary_top_k: int = Field(default=20, ge=3, le=50)
    rag_canary_final_k: int = Field(default=6, ge=1, le=12)
    # Canary RAG parameters are isolated from Stable.  The resolved immutable
    # strategy and its SHA are recorded with every response; changing one of
    # these values cannot silently mutate the Stable retrieval algorithm.
    rag_canary_rrf_k: int = Field(default=60, ge=1, le=200)
    rag_canary_dense_weight: float = Field(default=1.0, gt=0, le=5)
    rag_canary_sparse_weight: float = Field(default=1.0, gt=0, le=5)
    # Total weight shared across all query-expansion channels.
    rag_canary_expansion_weight: float = Field(default=0.75, gt=0, le=2)
    rag_canary_candidate_k: int = Field(default=20, ge=3, le=50)
    rag_canary_rerank_policy: Literal["disabled", "conditional", "always"] = "always"
    rag_canary_rerank_weight: float = Field(default=0.70, ge=0, le=0.9)
    rag_canary_context_chars: int = Field(default=12000, ge=2000, le=50000)
    reranker_enabled: bool = False
    reranker_provider: Literal["llm_json", "dashscope"] = "llm_json"
    reranker_model: str = ""
    reranker_candidate_k: int = Field(default=10, ge=3, le=20)
    reranker_min_candidates: int = Field(default=4, ge=2, le=20)
    reranker_weight: float = Field(default=0.55, ge=0, le=0.9)
    reranker_ambiguity_margin: float = Field(default=0.08, ge=0, le=0.5)
    reranker_timeout_seconds: float = Field(default=10, ge=1, le=60)
    reranker_max_concurrency: int = Field(default=8, ge=1, le=100)
    reranker_queue_timeout_seconds: float = Field(default=1.5, ge=0.1, le=30)
    max_context_chars: int = Field(default=12000, ge=2000, le=50000)
    max_history_chars: int = Field(default=6000, ge=1000, le=30000)
    max_long_memory_chars: int = Field(default=4000, ge=500, le=20000)
    history_turns: int = Field(default=6, ge=0, le=20)
    chat_turn_lease_seconds: int = Field(default=300, ge=30, le=1800)
    daily_question_limit: int = Field(default=100, ge=1, le=10000)
    question_max_chars: int = Field(default=500, ge=10, le=2000)
    fallback_message: str = "抱歉，当前知识库中没有找到足够可靠的相关信息。为避免误导，我暂时无法回答。请换一种问法，或联系人工客服补充资料。"

    @field_validator("secret_key")
    @classmethod
    def secure_production_secret(cls, value: SecretStr, info):
        raw = value.get_secret_value()
        if info.data.get("environment") == "production" and (
            len(raw) < 32 or raw.startswith("development-only")
        ):
            raise ValueError("production SECRET_KEY must be a non-default value of at least 32 characters")
        return value

    @model_validator(mode="after")
    def reject_insecure_production_bootstrap(self):
        if (
            self.environment == "production"
            and self.auto_seed
            and self.initial_admin_password.get_secret_value() == "Admin123!"
        ):
            raise ValueError("production auto-seeding requires a non-default INITIAL_ADMIN_PASSWORD")
        if self.environment == "production" and self.auto_create_tables:
            raise ValueError("production must use Alembic migrations; set AUTO_CREATE_TABLES=false")
        if self.ingestion_mode == "queue" and not self.qdrant_url:
            raise ValueError("queued ingestion requires external QDRANT_URL; local mode is single-process only")
        if (
            self.multimodal_provider == "openai_compatible"
            and not self.llm_api_key.get_secret_value()
            and not self.dashscope_api_key.get_secret_value()
        ):
            if not self.llm_base_url.startswith(("http://localhost", "http://127.0.0.1", "http://vllm")):
                raise ValueError("remote multimodal provider requires LLM_API_KEY")
        if self.retrieval_final_k > self.retrieval_top_k:
            raise ValueError("RETRIEVAL_FINAL_K cannot exceed RETRIEVAL_TOP_K")
        if self.reranker_candidate_k < self.retrieval_final_k:
            raise ValueError("RERANKER_CANDIDATE_K must be greater than or equal to RETRIEVAL_FINAL_K")
        if self.reranker_candidate_k > self.retrieval_top_k:
            raise ValueError("RERANKER_CANDIDATE_K cannot exceed RETRIEVAL_TOP_K")
        if self.reranker_min_candidates > self.reranker_candidate_k:
            raise ValueError("RERANKER_MIN_CANDIDATES cannot exceed RERANKER_CANDIDATE_K")
        if self.rag_canary_final_k > self.rag_canary_top_k:
            raise ValueError("RAG_CANARY_FINAL_K cannot exceed RAG_CANARY_TOP_K")
        if self.rag_canary_candidate_k < self.rag_canary_final_k:
            raise ValueError("RAG_CANARY_CANDIDATE_K cannot be less than RAG_CANARY_FINAL_K")
        if self.rag_canary_candidate_k > self.rag_canary_top_k:
            raise ValueError("RAG_CANARY_CANDIDATE_K cannot exceed RAG_CANARY_TOP_K")
        if self.reranker_min_candidates > self.rag_canary_candidate_k:
            raise ValueError("RERANKER_MIN_CANDIDATES cannot exceed RAG_CANARY_CANDIDATE_K")
        # Validate configured versions during settings construction so a typo
        # cannot reach live traffic and silently fall back to another algorithm.
        from app.services.rag_strategy import get_rag_strategy

        get_rag_strategy(self.rag_stable_version, self)
        get_rag_strategy(self.rag_canary_version, self)
        if self.embedding_canary_percent > 0:
            if not self.embedding_canary_model or not self.qdrant_canary_collection:
                raise ValueError(
                    "embedding canary traffic requires EMBEDDING_CANARY_MODEL and "
                    "QDRANT_CANARY_COLLECTION"
                )
            if self.qdrant_canary_collection == self.qdrant_collection:
                raise ValueError("stable and canary embeddings require different Qdrant collections")
        if self.environment == "production" and any(
            value > 0
            for value in (
                self.model_canary_percent,
                self.embedding_canary_percent,
                self.prompt_canary_percent,
                self.rag_canary_percent,
                self.service_canary_percent,
                self.shadow_canary_percent,
            )
        ) and self.experiment_salt.get_secret_value() == "development-experiment-salt":
            raise ValueError("production canary routing requires a non-default EXPERIMENT_SALT")
        if (
            self.llm_provider == "openai_compatible"
            and (self.service_canary_percent > 0 or self.shadow_canary_percent > 0)
            and not self.llm_canary_base_url
        ):
            raise ValueError("service/shadow canary traffic requires LLM_CANARY_BASE_URL")
        if (
            self.llm_provider == "openai_compatible"
            and (self.model_canary_percent > 0 or self.shadow_canary_percent > 0)
            and not self.llm_canary_model
        ):
            raise ValueError("model/shadow canary traffic requires LLM_CANARY_MODEL")
        return self

    @field_validator("business_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @property
    def origin_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def extension_set(self) -> set[str]:
        return {item.strip().lower() for item in self.allowed_extensions.split(",") if item.strip()}

    @property
    def experiment_whitelist(self) -> set[str]:
        return {item.strip() for item in self.experiment_whitelist_user_ids.split(",") if item.strip()}

    @property
    def experiment_tenant_whitelist(self) -> set[str]:
        return {
            item.strip()
            for item in self.experiment_whitelist_tenant_ids.split(",")
            if item.strip()
        }

    @property
    def experiment_conversation_whitelist(self) -> set[str]:
        return {
            item.strip()
            for item in self.experiment_whitelist_conversation_ids.split(",")
            if item.strip()
        }

    @property
    def experiment_blacklist(self) -> set[str]:
        return {item.strip() for item in self.experiment_blacklist_user_ids.split(",") if item.strip()}

    @property
    def experiment_tenant_blacklist(self) -> set[str]:
        return {
            item.strip()
            for item in self.experiment_blacklist_tenant_ids.split(",")
            if item.strip()
        }

    @property
    def experiment_conversation_blacklist(self) -> set[str]:
        return {
            item.strip()
            for item in self.experiment_blacklist_conversation_ids.split(",")
            if item.strip()
        }

    @property
    def stable_intent_set(self) -> set[str]:
        return {item.strip() for item in self.experiment_stable_intents.split(",") if item.strip()}

    @property
    def effective_experiment_salt(self) -> bytes:
        """Return a domain-separated routing secret without exposing either secret.

        Production startup still requires an explicit salt when percentages are
        configured in the environment.  This derivation additionally protects
        canaries enabled later through the database-backed administrator API.
        """
        configured = self.experiment_salt.get_secret_value()
        if configured != "development-experiment-salt":
            return configured.encode("utf-8")
        return hmac.new(
            self.secret_key.get_secret_value().encode("utf-8"),
            b"aegis/experiment-routing/v1",
            hashlib.sha256,
        ).digest()

    @property
    def provider_allowed_host_set(self) -> set[str]:
        return {item.strip().lower() for item in self.provider_allowed_hosts.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
