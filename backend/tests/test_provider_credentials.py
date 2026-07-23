from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import ProviderCredential, ProviderCredentialAudit
from app.services.provider_credentials import (
    ProviderValidation,
    api_key_fingerprint,
    decrypt_api_key,
    encrypt_api_key,
    validate_provider_url,
)
from tests.conftest import auth


def test_provider_secrets_are_encrypted_masked_and_admin_only(
    client: TestClient,
    admin_token: str,
    user_token: str,
):
    forbidden = client.get("/api/v1/admin/providers", headers=auth(user_token))
    assert forbidden.status_code == 403

    response = client.get("/api/v1/admin/providers", headers=auth(admin_token))
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {"llm_stable", "dashscope_ai"}
    assert all("api_key_ciphertext" not in item and "api_key" not in item for item in response.json())

    secret = "test-provider-key-with-high-entropy"
    ciphertext = encrypt_api_key(secret)
    assert secret not in ciphertext
    assert decrypt_api_key(ciphertext) == secret
    assert api_key_fingerprint(secret) == api_key_fingerprint(secret)
    assert len(api_key_fingerprint(secret)) == 12


def test_validated_update_is_atomic_audited_and_optimistically_locked(
    client: TestClient,
    admin_token: str,
    monkeypatch,
):
    import app.api.admin as admin_api

    async def validation_ok(config):
        assert config.api_key == "replacement-secret"
        return ProviderValidation(True, "valid", None, 12, config.model)

    monkeypatch.setattr(admin_api, "validate_runtime_provider", validation_ok)
    before = client.get("/api/v1/admin/providers", headers=auth(admin_token)).json()
    current = next(item for item in before if item["id"] == "llm_stable")
    payload = {
        "expected_version": current["version"],
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key": "replacement-secret",
        "enabled": True,
        "validate_before_save": True,
        "reason": "测试密钥安全轮换",
    }
    updated = client.patch(
        "/api/v1/admin/providers/llm_stable",
        json=payload,
        headers=auth(admin_token),
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == current["version"] + 1
    assert body["validation_status"] == "valid"
    assert body["enabled"] is True
    assert "replacement-secret" not in updated.text

    stale = client.patch(
        "/api/v1/admin/providers/llm_stable",
        json=payload,
        headers=auth(admin_token),
    )
    assert stale.status_code == 409
    with SessionLocal() as db:
        record = db.get(ProviderCredential, "llm_stable")
        assert record is not None
        assert decrypt_api_key(record.api_key_ciphertext) == "replacement-secret"
        audits = list(db.query(ProviderCredentialAudit).all())
        assert any(item.action == "validated_update" and item.succeeded for item in audits)
        assert all("replacement-secret" not in str(item.changed_fields) for item in audits)


def test_failed_validation_preserves_active_configuration_and_records_safe_error(
    client: TestClient,
    admin_token: str,
    monkeypatch,
):
    import app.api.admin as admin_api

    async def validation_failed(config):
        return ProviderValidation(False, "invalid", "invalid_api_key", 8, config.model)

    monkeypatch.setattr(admin_api, "validate_runtime_provider", validation_failed)
    current = next(
        item
        for item in client.get("/api/v1/admin/providers", headers=auth(admin_token)).json()
        if item["id"] == "llm_stable"
    )
    with SessionLocal() as db:
        before = db.get(ProviderCredential, "llm_stable")
        assert before is not None
        previous_ciphertext = before.api_key_ciphertext

    response = client.patch(
        "/api/v1/admin/providers/llm_stable",
        json={
            "expected_version": current["version"],
            "provider": current["provider"],
            "base_url": current["base_url"],
            "model": current["model"],
            "api_key": "invalid-replacement",
            "enabled": True,
            "validate_before_save": True,
            "reason": "测试失败不覆盖",
        },
        headers=auth(admin_token),
    )
    assert response.status_code == 422
    assert "invalid_api_key" in response.json()["detail"]
    with SessionLocal() as db:
        after = db.get(ProviderCredential, "llm_stable")
        assert after is not None
        assert after.version == current["version"]
        assert after.api_key_ciphertext == previous_ciphertext
        audit = db.query(ProviderCredentialAudit).order_by(ProviderCredentialAudit.created_at.desc()).first()
        assert audit is not None
        assert audit.action == "validation_failed"
        assert audit.error_code == "invalid_api_key"
        assert audit.succeeded is False


def test_provider_url_allowlist_prevents_credential_exfiltration():
    assert validate_provider_url("https://api.deepseek.com/") == "https://api.deepseek.com"
    assert validate_provider_url(
        "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    ).endswith("/compatible-mode/v1")
    for unsafe in (
        "http://api.deepseek.com",
        "https://api.deepseek.com@evil.example/v1",
        "https://evil.example/v1",
        "https://api.deepseek.com/v1?redirect=https://evil.example",
    ):
        try:
            validate_provider_url(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe provider URL was accepted: {unsafe}")
