from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import ProviderCredential


logger = logging.getLogger(__name__)

LLM_STABLE = "llm_stable"
DASHSCOPE_AI = "dashscope_ai"
SUPPORTED_CREDENTIAL_IDS = frozenset({LLM_STABLE, DASHSCOPE_AI})


@dataclass(frozen=True)
class RuntimeProvider:
    id: str
    provider: str
    base_url: str
    model: str
    api_key: str
    version: int
    enabled: bool


@dataclass(frozen=True)
class ProviderValidation:
    ok: bool
    status: str
    error_code: str | None
    latency_ms: int
    model: str


def _fernet() -> Fernet:
    settings = get_settings()
    configured = settings.credential_encryption_key.get_secret_value().strip()
    root = configured or settings.secret_key.get_secret_value()
    # Domain separation prevents the Fernet key from being the JWT signing key
    # even when a local deployment intentionally uses the same root secret.
    derived = hashlib.sha256(b"aegis-provider-credentials-v1\x00" + root.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_api_key(value: str) -> str:
    normalized = value.strip()
    return _fernet().encrypt(normalized.encode("utf-8")).decode("ascii") if normalized else ""


def decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise RuntimeError("provider credential cannot be decrypted with the configured key") from exc


def api_key_fingerprint(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    settings = get_settings()
    root = settings.credential_encryption_key.get_secret_value().strip() or settings.secret_key.get_secret_value()
    return hmac.new(root.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def validate_provider_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("API Host 必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API Host 不能包含账号、查询参数或片段")
    local_hosts = {"localhost", "127.0.0.1", "host.docker.internal", "vllm-stable", "vllm-canary", "ollama"}
    if parsed.scheme != "https" and hostname not in local_hosts:
        raise ValueError("远程模型服务必须使用 HTTPS")
    allowed = get_settings().provider_allowed_host_set
    if not any(
        hostname == entry or (entry.startswith(".") and hostname.endswith(entry))
        for entry in allowed
    ):
        raise ValueError("该 API Host 不在服务端允许列表中")
    return normalized


def public_credential(record: ProviderCredential) -> dict:
    return {
        "id": record.id,
        "display_name": record.display_name,
        "category": record.category,
        "provider": record.provider,
        "base_url": record.base_url,
        "model": record.model,
        "enabled": record.enabled,
        "has_api_key": bool(record.api_key_ciphertext),
        "api_key_fingerprint": record.api_key_fingerprint,
        "validation_status": record.validation_status,
        "last_error_code": record.last_error_code,
        "last_validated_at": record.last_validated_at,
        "version": record.version,
        "updated_at": record.updated_at,
    }


def ensure_provider_credentials() -> None:
    """Import ignored environment secrets once without overwriting admin state."""
    settings = get_settings()
    definitions = (
        {
            "id": LLM_STABLE,
            "display_name": "主对话模型",
            "category": "llm",
            "provider": "openai_compatible",
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "api_key": settings.llm_api_key.get_secret_value(),
            "enabled": settings.llm_provider == "openai_compatible",
        },
        {
            "id": DASHSCOPE_AI,
            "display_name": "阿里云百炼 AI 能力",
            "category": "ai_suite",
            "provider": "dashscope",
            "base_url": settings.dashscope_base_url,
            "model": settings.vision_model if settings.vision_model != "gpt-4o-mini" else "qwen3-vl-flash",
            "api_key": settings.dashscope_api_key.get_secret_value(),
            "enabled": bool(
                settings.dashscope_bootstrap_validated
                and settings.dashscope_api_key.get_secret_value()
            ),
        },
    )
    with SessionLocal() as db:
        for item in definitions:
            if db.get(ProviderCredential, item["id"]) is not None:
                continue
            values = dict(item)
            secret = str(values.pop("api_key"))
            values["base_url"] = validate_provider_url(str(values["base_url"]))
            db.add(
                ProviderCredential(
                    **values,
                    api_key_ciphertext=encrypt_api_key(secret),
                    api_key_fingerprint=api_key_fingerprint(secret),
                    validation_status=(
                        "valid"
                        if values.get("enabled") and item["id"] == DASHSCOPE_AI
                        else "unverified"
                    ),
                    last_validated_at=(
                        datetime.now(timezone.utc)
                        if values.get("enabled") and item["id"] == DASHSCOPE_AI
                        else None
                    ),
                )
            )
        db.commit()


def runtime_provider(
    db: Session,
    credential_id: str,
    *,
    require_enabled: bool = True,
) -> RuntimeProvider | None:
    record = db.get(ProviderCredential, credential_id)
    if record is None or (require_enabled and not record.enabled) or not record.api_key_ciphertext:
        return None
    return RuntimeProvider(
        id=record.id,
        provider=record.provider,
        base_url=record.base_url,
        model=record.model,
        api_key=decrypt_api_key(record.api_key_ciphertext),
        version=record.version,
        enabled=record.enabled,
    )


def current_runtime_provider(
    credential_id: str,
    *,
    require_enabled: bool = True,
) -> RuntimeProvider | None:
    try:
        with SessionLocal() as db:
            return runtime_provider(db, credential_id, require_enabled=require_enabled)
    except Exception:
        # Environment configuration remains an explicit emergency fallback. Do
        # not log exception values because some SDK/database errors echo input.
        logger.error("provider credential lookup failed id=%s", credential_id, exc_info=False)
        return None


def effective_stable_model(db: Session | None = None) -> str:
    settings = get_settings()
    if db is not None:
        configured = runtime_provider(db, LLM_STABLE)
    else:
        configured = current_runtime_provider(LLM_STABLE)
    return configured.model if configured else settings.llm_model


def _error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            value = error.get("code") or error.get("type")
            if value:
                return str(value)[:80]
    except Exception:
        pass
    return f"http_{response.status_code}"


async def validate_runtime_provider(config: RuntimeProvider) -> ProviderValidation:
    base_url = validate_provider_url(config.base_url)
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            if config.id == LLM_STABLE:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": config.model,
                        "messages": [{"role": "user", "content": "只回答：正常"}],
                        "temperature": 0,
                        "max_tokens": 16,
                        "stream": False,
                    },
                )
            else:
                settings = get_settings()
                origin = urlsplit(base_url)
                rerank_url = (
                    f"{origin.scheme}://{origin.netloc}/compatible-api/v1/reranks"
                )
                responses = await asyncio.gather(
                    client.post(
                        f"{base_url}/embeddings",
                        headers=headers,
                        json={
                            "model": settings.embedding_model,
                            "input": ["凭证能力验证"],
                            "dimensions": settings.embedding_dimensions,
                        },
                    ),
                    client.post(
                        rerank_url,
                        headers=headers,
                        json={
                            "model": settings.reranker_model or "qwen3-rerank",
                            "query": "退款",
                            "documents": ["退款规则", "电池规则"],
                            "top_n": 2,
                            "return_documents": False,
                        },
                    ),
                    client.post(
                        f"{base_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": config.model,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "客观描述图片主色，只回答颜色。"},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": (
                                                    "data:image/png;base64,"
                                                    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJ"
                                                    "AAAAfElEQVR4nNXOQREAMAjAsK7+PTMRPLhGQd7QJnESJ3ES"
                                                    "J3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3"
                                                    "ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ES"
                                                    "J3ES53Vg6wNShQF/fRSLfgAAAABJRU5ErkJggg=="
                                                )
                                            },
                                        },
                                    ],
                                }
                            ],
                            "temperature": 0,
                            "max_tokens": 8,
                        },
                    ),
                )
        latency_ms = round((time.perf_counter() - started) * 1000)
        if config.id == DASHSCOPE_AI:
            names = ("embedding", "reranker", "multimodal")
            for name, item in zip(names, responses, strict=True):
                if item.is_redirect:
                    return ProviderValidation(
                        False, "invalid", f"{name}_redirect_rejected", latency_ms, config.model
                    )
                if item.status_code != 200:
                    return ProviderValidation(
                        False,
                        "invalid",
                        f"{name}_{_error_code(item)}"[:80],
                        latency_ms,
                        config.model,
                    )
            try:
                embedding_data = responses[0].json()["data"]
                vector = embedding_data[0]["embedding"]
                rerank_results = responses[1].json()["results"]
                vision_choices = responses[2].json()["choices"]
                if len(embedding_data) != 1 or len(vector) != settings.embedding_dimensions:
                    raise ValueError("invalid embedding dimensions")
                if len(rerank_results) != 2:
                    raise ValueError("incomplete rerank response")
                if not vision_choices or not vision_choices[0]["message"]["content"]:
                    raise ValueError("empty vision response")
            except (KeyError, IndexError, TypeError, ValueError):
                return ProviderValidation(
                    False,
                    "invalid",
                    "capability_response_invalid",
                    latency_ms,
                    config.model,
                )
            return ProviderValidation(True, "valid", None, latency_ms, config.model)
        if response.is_redirect:
            return ProviderValidation(False, "invalid", "redirect_rejected", latency_ms, config.model)
        if response.status_code != 200:
            return ProviderValidation(False, "invalid", _error_code(response), latency_ms, config.model)
        return ProviderValidation(True, "valid", None, latency_ms, config.model)
    except httpx.TimeoutException:
        return ProviderValidation(False, "invalid", "timeout", round((time.perf_counter() - started) * 1000), config.model)
    except httpx.HTTPError:
        return ProviderValidation(False, "invalid", "connection_error", round((time.perf_counter() - started) * 1000), config.model)


def list_provider_credentials(db: Session) -> list[ProviderCredential]:
    return list(db.scalars(select(ProviderCredential).order_by(ProviderCredential.id)))
