from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Message, MessageRole
from app.services.memory import load_prompt_memory
from app.services.user_profile import apply_profile_retrieval_hints, learn_profile_facts


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_profile_facts_have_consent_provenance_expiry_and_revoke(
    client: TestClient,
    user_token: str,
):
    headers = auth(user_token)
    initial = client.get("/api/v1/profile", headers=headers)
    assert initial.status_code == 200
    assert initial.json()["profile_learning_consent"] is False
    assert initial.json()["facts"] == []
    assert initial.json()["service_summary"]["total_questions"] == 0

    enabled = client.put(
        "/api/v1/profile",
        headers=headers,
        json={
            "preferred_name": "小明",
            "locale": "zh-CN",
            "timezone": "Asia/Hong_Kong",
            "response_style": "balanced",
            "memory_consent": False,
            "consent_version": None,
            "profile_learning_consent": True,
            "profile_consent_version": "profile-v1",
        },
    )
    assert enabled.status_code == 200

    explicit = client.post(
        "/api/v1/profile/facts",
        headers=headers,
        json={"fact_type": "preferred_contact_channel", "value": "chat"},
    )
    assert explicit.status_code == 201
    assert explicit.json()["source"] == "explicit"
    assert explicit.json()["confidence"] == 10000

    rejected = client.post(
        "/api/v1/profile/facts",
        headers=headers,
        json={"fact_type": "device_model", "value": "验证码 123456"},
    )
    assert rejected.status_code == 422

    session_id = client.post(
        "/api/v1/sessions", headers=headers, json={"title": "画像学习"}
    ).json()["id"]
    user_id = client.get("/api/v1/auth/me", headers=headers).json()["id"]
    with SessionLocal() as db:
        message = Message(
            session_id=session_id,
            role=MessageRole.USER,
            content="我的小米 14 Ultra 是在京东买的，黑屏怎么办？",
            intent="售后问题",
        )
        db.add(message)
        db.flush()
        learned = learn_profile_facts(
            db,
            user_id=user_id,
            message_id=message.id,
            text=message.content,
        )
        db.commit()
        assert {fact.fact_type for fact in learned} == {"device_model", "purchase_channel"}
        profile_context, _, _ = load_prompt_memory(db, session_id, user_id)
        context = json.loads(profile_context)
        assert {fact["type"] for fact in context["verified_preferences_and_facts"]} == {
            "device_model",
            "purchase_channel",
            "preferred_contact_channel",
        }

    profile = client.get("/api/v1/profile", headers=headers).json()
    inferred = [fact for fact in profile["facts"] if fact["source"] == "conversation"]
    assert len(inferred) == 2
    assert all(fact["expires_at"] for fact in inferred)
    assert all(fact["source_message_id"] for fact in inferred)

    disabled = client.put(
        "/api/v1/profile",
        headers=headers,
        json={
            "preferred_name": "小明",
            "locale": "zh-CN",
            "timezone": "Asia/Hong_Kong",
            "response_style": "balanced",
            "memory_consent": False,
            "profile_learning_consent": False,
        },
    )
    assert disabled.status_code == 200
    assert [fact["source"] for fact in disabled.json()["facts"]] == ["explicit"]

    fact_id = explicit.json()["id"]
    forgotten = client.delete(f"/api/v1/profile/facts/{fact_id}", headers=headers)
    assert forgotten.status_code == 204
    assert client.get("/api/v1/profile", headers=headers).json()["facts"] == []


def test_profile_hints_only_qualify_ambiguous_questions():
    hints = {"device_model": "小米 14 Ultra", "purchase_channel": "third_party"}
    enriched = apply_profile_retrieval_hints("我的手机退货要找谁？", hints)
    assert "设备型号=小米 14 Ultra" in enriched
    assert "购买渠道=third_party" in enriched

    explicit = apply_profile_retrieval_hints("京东买的红米 Note 14 怎么退货？", hints)
    assert explicit == "京东买的红米 Note 14 怎么退货？"
