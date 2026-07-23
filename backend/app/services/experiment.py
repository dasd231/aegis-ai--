from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import ConversationSession


AXES = ("model", "embedding", "prompt", "rag", "service", "shadow")


@dataclass(frozen=True)
class ExperimentAssignment:
    group: str
    model_pool: str
    model_version: str
    embedding_pool: str
    embedding_version: str
    embedding_model: str
    embedding_dimensions: int
    vector_index_version: str
    prompt_version: str
    prompt_sha256: str
    rag_version: str
    service_pool: str
    service_version: str
    buckets: dict[str, int]
    reason: str
    shadow_percent: int = 0

    def public_dict(self) -> dict:
        data = asdict(self)
        # Buckets help reproduce a decision without exposing the routing salt.
        return data


def _bucket(settings: Settings, tenant_id: str, conversation_id: str, axis: str) -> int:
    material = f"{axis}:{tenant_id}:{conversation_id}".encode("utf-8")
    digest = hmac.new(settings.effective_experiment_salt, material, hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _is_canary(bucket: int, percentage: int) -> bool:
    return bucket < percentage * 100


def route_experiment(
    user_id: str,
    conversation_id: str,
    intent: str,
    *,
    tenant_id: str = "default",
    settings: Settings | None = None,
    stable_model: str | None = None,
) -> ExperimentAssignment:
    settings = settings or get_settings()
    buckets = {axis: _bucket(settings, tenant_id, conversation_id, axis) for axis in AXES}
    if (
        tenant_id in settings.experiment_tenant_blacklist
        or user_id in settings.experiment_blacklist
        or conversation_id in settings.experiment_conversation_blacklist
    ):
        choices = {axis: False for axis in AXES}
        reason = "explicit_blacklist"
    elif (
        tenant_id in settings.experiment_tenant_whitelist
        or user_id in settings.experiment_whitelist
        or conversation_id in settings.experiment_conversation_whitelist
    ):
        choices = {
            "model": bool(settings.llm_canary_model),
            "embedding": bool(
                settings.embedding_canary_model and settings.qdrant_canary_collection
            ),
            "prompt": True,
            "rag": True,
            "service": bool(settings.llm_canary_base_url),
        }
        reason = "explicit_whitelist"
    elif intent in settings.stable_intent_set:
        choices = {axis: False for axis in AXES}
        reason = "stable_intent"
    else:
        choices = {
            "model": _is_canary(buckets["model"], settings.model_canary_percent),
            "embedding": _is_canary(
                buckets["embedding"], settings.embedding_canary_percent
            ),
            "prompt": _is_canary(buckets["prompt"], settings.prompt_canary_percent),
            "rag": _is_canary(buckets["rag"], settings.rag_canary_percent),
            "service": _is_canary(buckets["service"], settings.service_canary_percent),
        }
        reason = "stable_hash"

    group = "canary" if any(choices.values()) else "stable"
    prompt_version = settings.prompt_canary_version if choices["prompt"] else settings.prompt_stable_version
    from app.services.prompt_registry import get_prompt_registry

    return ExperimentAssignment(
        group=group,
        model_pool="canary" if choices["model"] else "stable",
        model_version=(settings.llm_canary_model or settings.llm_model) if choices["model"] else (stable_model or settings.llm_model),
        embedding_pool="canary" if choices["embedding"] else "stable",
        embedding_version=(
            settings.embedding_canary_version
            if choices["embedding"]
            else settings.embedding_stable_version
        ),
        embedding_model=(
            settings.embedding_canary_model if choices["embedding"] else settings.embedding_model
        ),
        embedding_dimensions=(
            settings.embedding_canary_dimensions
            if choices["embedding"]
            else settings.embedding_dimensions
        ),
        vector_index_version=(
            settings.qdrant_canary_collection
            if choices["embedding"]
            else settings.qdrant_collection
        ),
        prompt_version=prompt_version,
        prompt_sha256=get_prompt_registry().get(prompt_version).sha256,
        rag_version=settings.rag_canary_version if choices["rag"] else settings.rag_stable_version,
        service_pool="canary" if choices["service"] else "stable",
        service_version=(
            settings.llm_service_canary_version
            if choices["service"]
            else settings.llm_service_stable_version
        ),
        buckets=buckets,
        reason=reason,
        shadow_percent=settings.shadow_canary_percent,
    )


def stable_assignment(
    settings: Settings | None = None,
    *,
    stable_model: str | None = None,
) -> ExperimentAssignment:
    settings = settings or get_settings()
    from app.services.prompt_registry import get_prompt_registry

    return ExperimentAssignment(
        group="stable",
        model_pool="stable",
        model_version=stable_model or settings.llm_model,
        embedding_pool="stable",
        embedding_version=settings.embedding_stable_version,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        vector_index_version=settings.qdrant_collection,
        prompt_version=settings.prompt_stable_version,
        prompt_sha256=get_prompt_registry().get(settings.prompt_stable_version).sha256,
        rag_version=settings.rag_stable_version,
        service_pool="stable",
        service_version=settings.llm_service_stable_version,
        buckets={},
        reason="default_stable",
        shadow_percent=0,
    )


def get_or_assign_experiment(
    db: Session,
    conversation: ConversationSession,
    user_id: str,
    intent: str,
    tenant_id: str = "default",
) -> ExperimentAssignment:
    """Freeze the first routing decision on the conversation.

    A configuration rollout therefore cannot move a live conversation between
    model, prompt, RAG, or vLLM pools halfway through a dialogue.
    """
    # Local import avoids a module cycle: the controller stores assignment metadata.
    from app.services.canary_control import get_control
    from app.services.prompt_registry import get_prompt_registry
    from app.services.provider_credentials import effective_stable_model

    control = get_control(db)
    base_settings = get_settings()
    routing_settings = base_settings.model_copy(
        update={
            "model_canary_percent": control.model_canary_percent,
            "embedding_canary_percent": control.embedding_canary_percent,
            "prompt_canary_percent": control.prompt_canary_percent,
            "rag_canary_percent": control.rag_canary_percent,
            "service_canary_percent": control.service_canary_percent,
            "shadow_canary_percent": control.shadow_canary_percent,
        }
    )
    current_stable_model = effective_stable_model(db)
    if not control.canary_enabled:
        assignment = stable_assignment(stable_model=current_stable_model)
        conversation.experiment_group = assignment.group
        conversation.model_pool = assignment.model_pool
        conversation.model_version = assignment.model_version
        conversation.embedding_pool = assignment.embedding_pool
        conversation.embedding_version = assignment.embedding_version
        conversation.embedding_model = assignment.embedding_model
        conversation.embedding_dimensions = assignment.embedding_dimensions
        conversation.vector_index_version = assignment.vector_index_version
        conversation.prompt_version = assignment.prompt_version
        conversation.prompt_sha256 = assignment.prompt_sha256
        conversation.rag_version = assignment.rag_version
        conversation.service_pool = assignment.service_pool
        conversation.service_version = assignment.service_version
        conversation.experiment_buckets = conversation.experiment_buckets or {}
        db.flush()
        return ExperimentAssignment(**{**asdict(assignment), "reason": "canary_kill_switch"})

    if conversation.experiment_group:
        return ExperimentAssignment(
            group=conversation.experiment_group,
            model_pool=conversation.model_pool or "stable",
            model_version=conversation.model_version or base_settings.llm_model,
            embedding_pool=conversation.embedding_pool or "stable",
            embedding_version=(
                conversation.embedding_version or base_settings.embedding_stable_version
            ),
            embedding_model=conversation.embedding_model or base_settings.embedding_model,
            embedding_dimensions=(
                conversation.embedding_dimensions or base_settings.embedding_dimensions
            ),
            vector_index_version=(
                conversation.vector_index_version or base_settings.qdrant_collection
            ),
            prompt_version=conversation.prompt_version or base_settings.prompt_stable_version,
            prompt_sha256=(
                conversation.prompt_sha256
                or get_prompt_registry().get(
                    conversation.prompt_version or base_settings.prompt_stable_version
                ).sha256
            ),
            rag_version=conversation.rag_version or base_settings.rag_stable_version,
            service_pool=conversation.service_pool or "stable",
            service_version=(
                conversation.service_version or base_settings.llm_service_stable_version
            ),
            buckets=conversation.experiment_buckets or {},
            reason="conversation_snapshot",
            shadow_percent=(control.shadow_canary_percent if control.canary_enabled else 0),
        )

    assignment = route_experiment(
        user_id,
        conversation.id,
        intent,
        tenant_id=tenant_id,
        settings=routing_settings,
        stable_model=current_stable_model,
    )
    conversation.experiment_group = assignment.group
    conversation.model_pool = assignment.model_pool
    conversation.model_version = assignment.model_version
    conversation.embedding_pool = assignment.embedding_pool
    conversation.embedding_version = assignment.embedding_version
    conversation.embedding_model = assignment.embedding_model
    conversation.embedding_dimensions = assignment.embedding_dimensions
    conversation.vector_index_version = assignment.vector_index_version
    conversation.prompt_version = assignment.prompt_version
    conversation.prompt_sha256 = assignment.prompt_sha256
    conversation.rag_version = assignment.rag_version
    conversation.service_pool = assignment.service_pool
    conversation.service_version = assignment.service_version
    conversation.experiment_buckets = assignment.buckets
    db.flush()
    return assignment


def retrieval_limits(assignment: ExperimentAssignment, settings: Settings | None = None) -> tuple[int, int]:
    settings = settings or get_settings()
    if assignment.rag_version == settings.rag_canary_version:
        return settings.rag_canary_top_k, settings.rag_canary_final_k
    return settings.retrieval_top_k, settings.retrieval_final_k
