"""Start the real ASGI lifespan and verify the seeded offline RAG path."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="aegis-smoke-") as temporary:
        root = Path(temporary)
        os.environ.update(
            {
                "ENVIRONMENT": "development",
                "DATABASE_URL": f"sqlite:///{(root / 'smoke.db').as_posix()}",
                "AUTO_CREATE_TABLES": "true",
                "AUTO_SEED": "true",
                "SECRET_KEY": "smoke-test-secret-key-with-more-than-32-characters",
                "INITIAL_ADMIN_EMAIL": "admin@example.com",
                "INITIAL_ADMIN_PASSWORD": "SmokeAdmin123!",
                "ALLOWED_HOSTS": "testserver",
                "UPLOAD_DIR": str(root / "uploads"),
                "QDRANT_PATH": str(root / "qdrant"),
                "LLM_PROVIDER": "mock",
                "EMBEDDING_PROVIDER": "local_hash",
            }
        )
        from fastapi.testclient import TestClient

        from app.main import app

        run_checks(TestClient, app)


def run_checks(test_client_class, app) -> None:
    with test_client_class(app) as client:
        live = client.get("/api/v1/health/ready")
        assert live.status_code == 200, live.text
        login = client.post(
            "/api/v1/auth/login",
            json={"account": "admin@example.com", "password": "SmokeAdmin123!"},
        )
        assert login.status_code == 200, login.text
        admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        documents = client.get("/api/v1/knowledge/documents", headers=admin_headers)
        assert documents.status_code == 200, documents.text
        seeded = documents.json()["items"]
        assert len(seeded) == 3, seeded
        assert all(item["status"] == "ready" for item in seeded), seeded

        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "smoke@example.com", "password": "SmokeUser123", "display_name": "Smoke"},
        )
        assert registered.status_code == 201, registered.text
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        session = client.post("/api/v1/sessions", json={"title": "新对话"}, headers=headers)
        assert session.status_code == 201, session.text
        streamed = client.post(
            "/api/v1/chat/stream",
            json={"session_id": session.json()["id"], "question": "小米手机充电无反应应该怎么排查？"},
            headers=headers,
        )
        assert streamed.status_code == 200, streamed.text
        assert "event: sources" in streamed.text
        assert "小米产品使用与故障排查.md" in streamed.text
        assert "event: token" in streamed.text
        assert "event: done" in streamed.text
        print("SMOKE_OK seeded_documents=3 rag_stream=complete source=小米产品使用与故障排查.md")


if __name__ == "__main__":
    main()
