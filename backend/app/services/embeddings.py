from __future__ import annotations

import asyncio
import hashlib
import math
import re
from abc import ABC, abstractmethod

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.core.config import get_settings
from app.services.provider_credentials import DASHSCOPE_AI, current_runtime_provider
from app.services.resilience import embedding_breaker, embedding_bulkhead


class EmbeddingService(ABC):
    @abstractmethod
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]

    async def close(self) -> None:
        return None


class LocalHashEmbedding(EmbeddingService):
    """Dependency-free deterministic embedding for offline demos and tests.

    It is lexical rather than a production semantic model. Feature hashing keeps
    English words and CJK uni/bi-grams, which makes the complete RAG path usable
    without downloading a model or exposing an API key.
    """

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    @staticmethod
    def _tokens(text: str) -> list[str]:
        lowered = text.lower()
        words = re.findall(r"[a-z0-9_]+", lowered)
        cjk_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
        cjk = [char for run in cjk_runs for char in run]
        bigrams = [run[i : i + 2] for run in cjk_runs for i in range(max(0, len(run) - 1))]
        return words + cjk + bigrams

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dimensions
            sign = 1.0 if value & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]


class OpenAIEmbedding(EmbeddingService):
    def __init__(self, pool: str = "stable"):
        if pool not in {"stable", "canary"}:
            raise ValueError(f"unknown embedding pool: {pool}")
        settings = get_settings()
        self.settings = settings
        self.pool = pool
        canary = pool == "canary"
        self.model = settings.embedding_canary_model if canary else settings.embedding_model
        self.dimensions = (
            settings.embedding_canary_dimensions if canary else settings.embedding_dimensions
        )
        self.send_dimensions = (
            settings.embedding_canary_send_dimensions if canary else settings.embedding_send_dimensions
        )
        self.max_retries = settings.embedding_max_retries
        self.batch_size = (
            settings.embedding_canary_batch_size if canary else settings.embedding_batch_size
        )
        if canary and not self.model:
            raise RuntimeError("canary embedding pool selected without EMBEDDING_CANARY_MODEL")
        self._clients: dict[tuple, AsyncOpenAI] = {}
        base_url, key = self._environment_endpoint()
        self.client = self._make_client(base_url, key)
        self._clients[("environment", base_url)] = self.client

    def _environment_endpoint(self) -> tuple[str, str]:
        settings = self.settings
        canary = self.pool == "canary"
        explicit_key = (
            settings.embedding_canary_api_key.get_secret_value()
            if canary
            else settings.embedding_api_key.get_secret_value()
        )
        explicit_base = (
            settings.embedding_canary_base_url.strip()
            if canary
            else settings.embedding_base_url.strip()
        )
        if explicit_key or explicit_base:
            return explicit_base or settings.llm_base_url, explicit_key or settings.llm_api_key.get_secret_value()
        dashscope_key = settings.dashscope_api_key.get_secret_value()
        if dashscope_key:
            return settings.dashscope_base_url, dashscope_key
        return settings.llm_base_url, settings.llm_api_key.get_secret_value() or "local-embedding"

    def _make_client(self, base_url: str, api_key: str) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key or "local-embedding",
            base_url=base_url,
            timeout=self.settings.embedding_timeout_seconds,
            max_retries=0,
        )

    def _request_client(self) -> AsyncOpenAI:
        """Resolve the validated suite credential once per embedding request.

        Explicit EMBEDDING_* settings remain an isolated emergency endpoint.
        Otherwise an existing disabled database record is authoritative, so an
        administrator can actually stop remote embedding traffic.
        """
        explicit = bool(
            (
                self.settings.embedding_canary_api_key.get_secret_value()
                or self.settings.embedding_canary_base_url.strip()
            )
            if self.pool == "canary"
            else (
                self.settings.embedding_api_key.get_secret_value()
                or self.settings.embedding_base_url.strip()
            )
        )
        if explicit:
            return self.client
        configured = current_runtime_provider(DASHSCOPE_AI, require_enabled=False)
        if configured is None:
            return self.client
        if not configured.enabled:
            raise RuntimeError("DashScope AI suite is disabled by an administrator")
        identity = ("database", configured.id, configured.version, configured.base_url)
        client = self._clients.get(identity)
        if client is None:
            client = self._make_client(configured.base_url, configured.api_key)
            self._clients[identity] = client
        return client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = await asyncio.to_thread(self._request_client)
        vectors: list[list[float]] = []
        client_base_url = str(getattr(client, "base_url", "")).rstrip("/")
        target = f"{self.model}@{client_base_url}" if client_base_url else self.model
        breaker = embedding_breaker(self.pool, target=target)
        retryable = (RateLimitError, APITimeoutError, APIConnectionError)
        async with embedding_bulkhead(self.pool, target=target).slot():
            circuit_call = breaker.before_call()
            try:
                for start in range(0, len(texts), self.batch_size):
                    batch = texts[start : start + self.batch_size]
                    request = {"model": self.model, "input": batch}
                    if self.send_dimensions:
                        request["dimensions"] = self.dimensions
                    for attempt in range(self.max_retries + 1):
                        try:
                            response = await client.embeddings.create(**request)
                            break
                        except retryable:
                            if attempt >= self.max_retries:
                                raise
                            await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                    vectors.extend(
                        item.embedding for item in sorted(response.data, key=lambda item: item.index)
                    )
                if len(vectors) != len(texts):
                    raise ValueError("embedding provider returned an unexpected vector count")
                if any(
                    len(vector) != self.dimensions
                    or any(not math.isfinite(float(value)) for value in vector)
                    for vector in vectors
                ):
                    raise ValueError("embedding provider returned an invalid vector")
                breaker.succeeded(circuit_call)
                return vectors
            except asyncio.CancelledError:
                breaker.abandoned(circuit_call)
                raise
            except Exception:
                breaker.failed(circuit_call)
                raise

    async def close(self) -> None:
        clients = list({id(client): client for client in self._clients.values()}.values())
        self._clients.clear()
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


def create_embedding_service(pool: str = "stable") -> EmbeddingService:
    settings = get_settings()
    if settings.embedding_provider == "openai":
        return OpenAIEmbedding(pool)
    dimensions = (
        settings.embedding_canary_dimensions if pool == "canary" else settings.embedding_dimensions
    )
    return LocalHashEmbedding(dimensions)
