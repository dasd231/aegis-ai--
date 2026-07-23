from __future__ import annotations

from dataclasses import replace

from pydantic import SecretStr

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import ConversationSession, User
from app.services.canary_control import get_control, record_canary_observation
from app.services.experiment import get_or_assign_experiment, route_experiment, stable_assignment
from app.services.prompt_registry import get_prompt_registry
from tests.conftest import auth


def _settings(**updates):
    defaults = {
        "experiment_salt": SecretStr("test-routing-salt"),
        "experiment_whitelist_user_ids": "",
        "experiment_blacklist_user_ids": "",
        "experiment_stable_intents": "投诉,售后问题",
        "model_canary_percent": 0,
        "embedding_canary_percent": 0,
        "prompt_canary_percent": 0,
        "rag_canary_percent": 0,
        "service_canary_percent": 0,
        "llm_canary_model": "canary-model",
        "llm_canary_base_url": "http://vllm-canary:8000/v1",
        "embedding_canary_model": "canary-embedding",
        "embedding_canary_dimensions": 768,
        "qdrant_canary_collection": "knowledge_canary_768_v1",
    }
    defaults.update(updates)
    return get_settings().model_copy(update=defaults)


def test_router_is_stable_and_axes_are_independently_versioned():
    settings = _settings(
        model_canary_percent=100,
        embedding_canary_percent=100,
        prompt_canary_percent=0,
        rag_canary_percent=100,
        service_canary_percent=0,
    )
    first = route_experiment("tenant-user", "conversation-1", "产品咨询", settings=settings)
    second = route_experiment("tenant-user", "conversation-1", "产品咨询", settings=settings)

    assert first == second
    assert first.group == "canary"
    assert first.model_pool == "canary"
    assert first.model_version == "canary-model"
    assert first.embedding_pool == "canary"
    assert first.embedding_model == "canary-embedding"
    assert first.embedding_dimensions == 768
    assert first.vector_index_version == "knowledge_canary_768_v1"
    assert first.prompt_version == settings.prompt_stable_version
    assert first.rag_version == settings.rag_canary_version
    assert first.service_pool == "stable"
    assert set(first.buckets) == {"model", "embedding", "prompt", "rag", "service", "shadow"}


def test_sensitive_intent_and_blacklist_fail_closed_to_stable():
    all_canary = _settings(
        model_canary_percent=100,
        prompt_canary_percent=100,
        rag_canary_percent=100,
        service_canary_percent=100,
    )
    sensitive = route_experiment("u1", "c1", "售后问题", settings=all_canary)
    assert sensitive.group == "stable"
    assert sensitive.reason == "stable_intent"

    blacklisted = all_canary.model_copy(update={"experiment_blacklist_user_ids": "u1"})
    blocked = route_experiment("u1", "c2", "产品咨询", settings=blacklisted)
    assert blocked.group == "stable"
    assert blocked.reason == "explicit_blacklist"


def test_tenant_and_conversation_lists_override_percentage_routing():
    settings = _settings(
        model_canary_percent=0,
        experiment_whitelist_tenant_ids="tenant-canary",
        experiment_blacklist_conversation_ids="conversation-stable",
    )
    canary = route_experiment(
        "user-1",
        "conversation-canary",
        "产品咨询",
        tenant_id="tenant-canary",
        settings=settings,
    )
    blocked = route_experiment(
        "user-1",
        "conversation-stable",
        "产品咨询",
        tenant_id="tenant-canary",
        settings=settings,
    )

    assert canary.group == "canary"
    assert canary.reason == "explicit_whitelist"
    assert blocked.group == "stable"
    assert blocked.reason == "explicit_blacklist"


def test_hash_bucket_depends_on_tenant_and_conversation_not_user():
    settings = _settings(model_canary_percent=50)
    first = route_experiment(
        "user-a", "conversation-1", "产品咨询", tenant_id="tenant-1", settings=settings
    )
    second = route_experiment(
        "user-b", "conversation-1", "产品咨询", tenant_id="tenant-1", settings=settings
    )
    other_tenant = route_experiment(
        "user-a", "conversation-1", "产品咨询", tenant_id="tenant-2", settings=settings
    )

    assert first.buckets == second.buckets
    assert first.buckets != other_tenant.buckets


def test_conversation_assignment_is_frozen_after_first_turn():
    with SessionLocal() as db:
        user = User(
            email="experiment@example.com",
            password_hash="not-used",
            display_name="Experiment",
        )
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id, title="experiment")
        db.add(conversation)
        db.flush()

        first = get_or_assign_experiment(db, conversation, user.id, "产品咨询")
        conversation.prompt_version = "frozen_prompt"
        db.flush()
        second = get_or_assign_experiment(db, conversation, user.id, "投诉")

        assert second.group == first.group
        assert second.prompt_version == "frozen_prompt"
        assert second.reason == "conversation_snapshot"


def test_prompt_registry_has_reviewed_stable_and_canary_versions():
    registry = get_prompt_registry()
    stable = registry.get("customer_v1")
    canary = registry.get("customer_v2")

    assert stable.sha256 != canary.sha256
    assert "<evidence>" in stable.content
    assert "不得伪造" in canary.content


def test_canary_observations_trip_distributed_kill_switch():
    settings = get_settings()
    originals = (
        settings.canary_rollback_min_samples,
        settings.canary_rollback_error_rate,
        settings.canary_rollback_timeout_rate,
    )
    settings.canary_rollback_min_samples = 5
    settings.canary_rollback_error_rate = 0.5
    settings.canary_rollback_timeout_rate = 1.0
    assignment = replace(stable_assignment(), group="canary", prompt_version="customer_v2")
    try:
        for _ in range(5):
            record_canary_observation(
                assignment,
                success=False,
                latency_ms=100,
                ttft_ms=None,
                error_code="APIConnectionError",
            )
        with SessionLocal() as db:
            control = get_control(db)
            assert control.canary_enabled is False
            assert control.disabled_reason is not None
            assert "error_rate=" in control.disabled_reason
    finally:
        (
            settings.canary_rollback_min_samples,
            settings.canary_rollback_error_rate,
            settings.canary_rollback_timeout_rate,
        ) = originals


def test_admin_can_disable_and_reenable_canary_with_optimistic_lock(client, admin_token):
    current = client.get("/api/v1/admin/experiments/control", headers=auth(admin_token))
    assert current.status_code == 200
    assert current.json()["canary_enabled"] is True
    version = current.json()["version"]

    disabled = client.patch(
        "/api/v1/admin/experiments/control",
        json={
            "expected_version": version,
            "canary_enabled": False,
            "stage": 3,
            "model_canary_percent": 1,
            "embedding_canary_percent": 1,
            "prompt_canary_percent": 1,
            "rag_canary_percent": 1,
            "service_canary_percent": 1,
            "shadow_canary_percent": 100,
            "reason": "验收回滚演练",
        },
        headers=auth(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["canary_enabled"] is False
    assert disabled.json()["embedding_canary_percent"] == 1
    assert disabled.json()["stage"] == 3

    audits = client.get("/api/v1/admin/experiments/audits", headers=auth(admin_token))
    assert audits.status_code == 200
    assert audits.json()[0]["action"] == "manual_update"
    assert audits.json()[0]["next_config"]["embedding_canary_percent"] == 1

    stale = client.patch(
        "/api/v1/admin/experiments/control",
        json={"expected_version": version, "canary_enabled": True, "reason": "过期操作"},
        headers=auth(admin_token),
    )
    assert stale.status_code == 409

    enabled = client.patch(
        "/api/v1/admin/experiments/control",
        json={
            "expected_version": disabled.json()["version"],
            "canary_enabled": True,
            "stage": 0,
            "model_canary_percent": 0,
            "embedding_canary_percent": 0,
            "prompt_canary_percent": 0,
            "rag_canary_percent": 0,
            "service_canary_percent": 0,
            "shadow_canary_percent": 0,
            "reason": "回滚演练完成并通过质量门",
        },
        headers=auth(admin_token),
    )
    assert enabled.status_code == 200
    assert enabled.json()["canary_enabled"] is True


def test_admin_rejects_unconfigured_embedding_canary(client, admin_token):
    current = client.get("/api/v1/admin/experiments/control", headers=auth(admin_token)).json()
    response = client.patch(
        "/api/v1/admin/experiments/control",
        json={
            "expected_version": current["version"],
            "canary_enabled": True,
            "embedding_canary_percent": 1,
            "reason": "验证配置前置条件",
        },
        headers=auth(admin_token),
    )
    assert response.status_code == 422
    assert "Embedding" in response.json()["detail"]


def test_kill_switch_forces_shadow_percentage_to_zero():
    assignment = stable_assignment()
    assert assignment.shadow_percent == 0
