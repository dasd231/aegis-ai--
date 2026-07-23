from __future__ import annotations

# ruff: noqa: E402 -- test environment must be set before importing the application.

import os
import tempfile
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="aegis-tests-"))
os.environ.update(
    {
        "ENVIRONMENT": "test",
        # A real temporary SQLite file gives each worker thread its own
        # connection. A StaticPool-backed in-memory database lets concurrent
        # SSE lease renewal and answer persistence share one DBAPI transaction,
        # which can hide or roll back each other's writes and is not production-like.
        "DATABASE_URL": f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}",
        "AUTO_CREATE_TABLES": "true",
        "AUTO_SEED": "false",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-for-tests",
        "QDRANT_PATH": str(TEST_ROOT / "qdrant"),
        "UPLOAD_DIR": str(TEST_ROOT / "uploads"),
        "LLM_PROVIDER": "mock",
        "EMBEDDING_PROVIDER": "local_hash",
        "MULTIMODAL_PROVIDER": "disabled",
        "RERANKER_ENABLED": "false",
        "RERANKER_PROVIDER": "llm_json",
        "DASHSCOPE_API_KEY": "",
        "DASHSCOPE_BOOTSTRAP_VALIDATED": "false",
        "EMBEDDING_CANARY_MODEL": "",
        "QDRANT_CANARY_COLLECTION": "",
        "EMBEDDING_CANARY_PERCENT": "0",
        "ALLOWED_HOSTS": "testserver,localhost",
        "ALLOWED_ORIGINS": "http://localhost:5173",
    }
)

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import User, UserRole


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    from app.services.vector_store import get_vector_store

    get_vector_store().clear_all()
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(client: TestClient) -> str:
    with SessionLocal() as db:
        admin = User(
            email="admin@example.com",
            password_hash=hash_password("Admin123!"),
            display_name="Admin",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.commit()
    response = client.post("/api/v1/auth/login", json={"account": "admin@example.com", "password": "Admin123!"})
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture
def user_token(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "Secure123", "display_name": "Tester"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
