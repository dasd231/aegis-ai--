from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import ConversationSession, LoginThrottle, utcnow
from app.services.turn_lease import acquire_session_turn, release_session_turn, renew_session_turn
from tests.conftest import auth


def test_register_login_and_duplicate_protection(client: TestClient):
    payload = {"email": "someone@example.com", "password": "Valid123", "display_name": "Someone"}
    created = client.post("/api/v1/auth/register", json=payload)
    assert created.status_code == 201
    assert created.json()["user"]["email"] == "someone@example.com"
    assert client.post("/api/v1/auth/register", json=payload).status_code == 409

    logged_in = client.post(
        "/api/v1/auth/login", json={"account": "someone@example.com", "password": "Valid123"}
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["access_token"]
    assert client.post(
        "/api/v1/auth/login", json={"account": "someone@example.com", "password": "wrong"}
    ).status_code == 401


def test_login_throttle_is_distributed_enumeration_safe_and_clears_on_success(client: TestClient):
    from app.core.config import get_settings

    settings = get_settings()
    original_max = settings.login_max_failures
    original_lock = settings.login_lock_seconds
    settings.login_max_failures = 3
    settings.login_lock_seconds = 120
    try:
        account = "throttled@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": account, "password": "Valid123", "display_name": "Throttle"},
        )
        for _ in range(2):
            assert client.post(
                "/api/v1/auth/login", json={"account": account, "password": "wrong"}
            ).status_code == 401
        locked = client.post("/api/v1/auth/login", json={"account": account, "password": "wrong"})
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0
        assert client.post(
            "/api/v1/auth/login", json={"account": account, "password": "Valid123"}
        ).status_code == 429

        with SessionLocal() as db:
            throttle = db.query(LoginThrottle).one()
            assert account not in throttle.key_hash
            throttle.locked_until = utcnow() - timedelta(seconds=1)
            throttle.window_started_at = utcnow() - timedelta(seconds=settings.login_failure_window_seconds + 1)
            db.commit()
        succeeded = client.post(
            "/api/v1/auth/login", json={"account": account, "password": "Valid123"}
        )
        assert succeeded.status_code == 200
        with SessionLocal() as db:
            assert db.query(LoginThrottle).count() == 0

        # Unknown identities receive the same failure/lock behavior and never
        # reveal whether an account exists.
        unknown = "missing@example.com"
        for _ in range(2):
            assert client.post(
                "/api/v1/auth/login", json={"account": unknown, "password": "wrong"}
            ).status_code == 401
        assert client.post(
            "/api/v1/auth/login", json={"account": unknown, "password": "wrong"}
        ).status_code == 429
    finally:
        settings.login_max_failures = original_max
        settings.login_lock_seconds = original_lock


def test_session_isolation_between_users(client: TestClient, user_token: str):
    created = client.post("/api/v1/sessions", json={"title": "private"}, headers=auth(user_token))
    session_id = created.json()["id"]
    other = client.post(
        "/api/v1/auth/register",
        json={"phone": "+85255556666", "password": "Other123", "display_name": "Other"},
    ).json()["access_token"]
    assert client.get(f"/api/v1/sessions/{session_id}", headers=auth(other)).status_code == 404
    assert client.delete(f"/api/v1/sessions/{session_id}", headers=auth(other)).status_code == 404


def test_feedback_only_accepts_owned_assistant_messages(client: TestClient, user_token: str):
    session_id = client.post("/api/v1/sessions", json={"title": "新对话"}, headers=auth(user_token)).json()["id"]
    streamed = client.post(
        "/api/v1/chat/stream",
        json={"session_id": session_id, "question": "量子火箭的保修规则是什么？"},
        headers=auth(user_token),
    )
    assert streamed.status_code == 200
    detail = client.get(f"/api/v1/sessions/{session_id}", headers=auth(user_token)).json()
    assistant = next(message for message in detail["messages"] if message["role"] == "assistant")
    feedback = client.put(
        f"/api/v1/messages/{assistant['id']}/feedback",
        json={"rating": 1, "comment": "边界清楚"},
        headers=auth(user_token),
    )
    assert feedback.status_code == 200
    assert feedback.json()["rating"] == 1
    user_message = next(message for message in detail["messages"] if message["role"] == "user")
    assert client.put(
        f"/api/v1/messages/{user_message['id']}/feedback",
        json={"rating": 1},
        headers=auth(user_token),
    ).status_code == 404


def test_cors_preflight_allows_patch_for_configured_frontend(client: TestClient):
    response = client.options(
        "/api/v1/profile",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_session_turn_lease_blocks_parallel_turn_and_delete(client: TestClient, user_token: str):
    session_id = client.post(
        "/api/v1/sessions",
        json={"title": "lease"},
        headers=auth(user_token),
    ).json()["id"]
    user_id = client.get("/api/v1/auth/me", headers=auth(user_token)).json()["id"]
    with SessionLocal() as db:
        lease = acquire_session_turn(db, session_id, user_id)
    assert lease is not None
    assert renew_session_turn(lease) is True

    blocked = client.post(
        "/api/v1/chat/stream",
        json={"session_id": session_id, "question": "并发问题"},
        headers=auth(user_token),
    )
    assert blocked.status_code == 409
    assert blocked.headers["retry-after"] == "2"
    assert client.get("/api/v1/chat/usage", headers=auth(user_token)).json()["used"] == 0
    assert client.delete(f"/api/v1/sessions/{session_id}", headers=auth(user_token)).status_code == 409

    assert release_session_turn(lease) is True
    answered = client.post(
        "/api/v1/chat/stream",
        json={"session_id": session_id, "question": "恢复后的问题"},
        headers=auth(user_token),
    )
    assert answered.status_code == 200
    assert "event: done" in answered.text
    assert client.delete(f"/api/v1/sessions/{session_id}", headers=auth(user_token)).status_code == 204


def test_expired_session_turn_lease_can_be_reclaimed(client: TestClient, user_token: str):
    session_id = client.post(
        "/api/v1/sessions",
        json={"title": "expired lease"},
        headers=auth(user_token),
    ).json()["id"]
    user_id = client.get("/api/v1/auth/me", headers=auth(user_token)).json()["id"]
    with SessionLocal() as db:
        first = acquire_session_turn(db, session_id, user_id)
        session = db.get(ConversationSession, session_id)
        session.turn_lock_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert first is not None
    with SessionLocal() as db:
        second = acquire_session_turn(db, session_id, user_id)
    assert second is not None and second.token != first.token
    assert release_session_turn(first) is False
    assert release_session_turn(second) is True
