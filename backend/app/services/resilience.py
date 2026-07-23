from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager

from app.core.config import get_settings


class CircuitOpenError(RuntimeError):
    pass


class BulkheadFullError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 30) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at: float | None = None
        self._probe_in_flight = False
        self._generation = 0
        self._lock = threading.Lock()

    def before_call(self) -> int:
        with self._lock:
            if self.opened_at is None:
                return self._generation
            if time.monotonic() - self.opened_at < self.reset_seconds:
                raise CircuitOpenError("dependency circuit is open")
            # Half-open state admits exactly one probe. This avoids a recovery
            # stampede as soon as the cool-down window expires.
            if self._probe_in_flight:
                raise CircuitOpenError("dependency circuit is half-open")
            self._probe_in_flight = True
            return self._generation

    def succeeded(self, call_generation: int) -> None:
        with self._lock:
            if call_generation != self._generation:
                return
            self.failures = 0
            self.opened_at = None
            self._probe_in_flight = False

    def failed(self, call_generation: int) -> None:
        with self._lock:
            if call_generation != self._generation:
                return
            if self._probe_in_flight:
                self.failures = self.failure_threshold
                self.opened_at = time.monotonic()
                self._probe_in_flight = False
                self._generation += 1
                return
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()
                self._generation += 1

    def abandoned(self, call_generation: int) -> None:
        """Release a cancelled half-open probe while keeping the circuit open."""
        with self._lock:
            if call_generation == self._generation and self._probe_in_flight:
                self.opened_at = time.monotonic()
                self._probe_in_flight = False
                self._generation += 1

    @property
    def state(self) -> str:
        with self._lock:
            if self.opened_at is None:
                return "closed"
            if time.monotonic() - self.opened_at >= self.reset_seconds:
                return "half_open"
            return "open"


class AsyncBulkhead:
    def __init__(self, limit: int, wait_seconds: float) -> None:
        self.limit = limit
        self.wait_seconds = wait_seconds
        self._semaphore = asyncio.Semaphore(limit)
        self.active = 0
        self.rejected = 0

    @asynccontextmanager
    async def slot(self):
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.wait_seconds)
        except TimeoutError as exc:
            self.rejected += 1
            raise BulkheadFullError("dependency bulkhead is full") from exc
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._semaphore.release()


_registry_lock = threading.Lock()
_breakers: dict[tuple[str, str, str], CircuitBreaker] = {}
_bulkheads: dict[tuple[str, str, str], AsyncBulkhead] = {}

_BREAKER_PROFILES: dict[str, tuple[int, float]] = {
    "llm": (3, 30),
    "reranker": (3, 20),
    "embedding": (3, 20),
}


def _resource_key(capability: str, pool: str, target: str | None) -> tuple[str, str, str]:
    normalized_pool = pool.strip().lower() if pool and pool.strip() else "stable"
    normalized_target = target.strip() if target and target.strip() else "default"
    return capability, normalized_pool, normalized_target


def _get_breaker(capability: str, pool: str, target: str | None) -> CircuitBreaker:
    key = _resource_key(capability, pool, target)
    with _registry_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            failure_threshold, reset_seconds = _BREAKER_PROFILES[capability]
            breaker = CircuitBreaker(failure_threshold, reset_seconds)
            _breakers[key] = breaker
        return breaker


def _bulkhead_profile(capability: str) -> tuple[int, float]:
    settings = get_settings()
    if capability == "llm":
        return settings.llm_max_concurrency, settings.llm_queue_timeout_seconds
    if capability == "reranker":
        return settings.reranker_max_concurrency, settings.reranker_queue_timeout_seconds
    return settings.embedding_max_concurrency, settings.embedding_queue_timeout_seconds


def _get_bulkhead(capability: str, pool: str, target: str | None) -> AsyncBulkhead:
    key = _resource_key(capability, pool, target)
    with _registry_lock:
        bulkhead = _bulkheads.get(key)
        if bulkhead is None:
            limit, wait_seconds = _bulkhead_profile(capability)
            bulkhead = AsyncBulkhead(limit, wait_seconds)
            _bulkheads[key] = bulkhead
        return bulkhead


def llm_breaker(pool: str = "stable", target: str | None = None) -> CircuitBreaker:
    return _get_breaker("llm", pool, target)


def llm_bulkhead(pool: str = "stable", target: str | None = None) -> AsyncBulkhead:
    return _get_bulkhead("llm", pool, target)


def reranker_breaker(pool: str = "stable", target: str | None = None) -> CircuitBreaker:
    return _get_breaker("reranker", pool, target)


def reranker_bulkhead(pool: str = "stable", target: str | None = None) -> AsyncBulkhead:
    return _get_bulkhead("reranker", pool, target)


def embedding_breaker(pool: str = "stable", target: str | None = None) -> CircuitBreaker:
    return _get_breaker("embedding", pool, target)


def embedding_bulkhead(pool: str = "stable", target: str | None = None) -> AsyncBulkhead:
    return _get_bulkhead("embedding", pool, target)


def _aggregate_pool(
    capability: str,
    pool: str,
    breakers: dict[tuple[str, str, str], CircuitBreaker],
    bulkheads: dict[tuple[str, str, str], AsyncBulkhead],
) -> dict[str, int | float | str]:
    matching_breakers = [
        breaker
        for (item_capability, item_pool, _), breaker in breakers.items()
        if item_capability == capability and item_pool == pool
    ]
    matching_bulkheads = [
        bulkhead
        for (item_capability, item_pool, _), bulkhead in bulkheads.items()
        if item_capability == capability and item_pool == pool
    ]
    states = {breaker.state for breaker in matching_breakers}
    if "open" in states:
        circuit = "open"
    elif "half_open" in states:
        circuit = "half_open"
    else:
        circuit = "closed"
    configured_limit, configured_wait = _bulkhead_profile(capability)
    targets = {
        target
        for item_capability, item_pool, target in (*breakers.keys(), *bulkheads.keys())
        if item_capability == capability and item_pool == pool
    }
    return {
        "circuit": circuit,
        "open_circuits": sum(breaker.state != "closed" for breaker in matching_breakers),
        "active": sum(bulkhead.active for bulkhead in matching_bulkheads),
        "limit": sum(bulkhead.limit for bulkhead in matching_bulkheads) or configured_limit,
        "queue_timeout_seconds": configured_wait,
        "rejected_total": sum(bulkhead.rejected for bulkhead in matching_bulkheads),
        "target_count": len(targets),
    }


def resilience_snapshot() -> dict[str, object]:
    # Copy the registries under their lock, then inspect independent resources
    # without keeping registration on the health-check critical path.
    with _registry_lock:
        breakers = dict(_breakers)
        bulkheads = dict(_bulkheads)
    pools = {
        capability: {
            pool: _aggregate_pool(capability, pool, breakers, bulkheads)
            for pool in ("stable", "canary")
        }
        for capability in ("llm", "reranker", "embedding")
    }
    # Preserve the original stable-only keys for existing dashboards and smoke
    # tests while exposing the pool-aware view to new operational consumers.
    stable_llm = pools["llm"]["stable"]
    stable_reranker = pools["reranker"]["stable"]
    stable_embedding = pools["embedding"]["stable"]
    return {
        "llm_circuit": stable_llm["circuit"],
        "llm_active": stable_llm["active"],
        "llm_limit": stable_llm["limit"],
        "reranker_circuit": stable_reranker["circuit"],
        "reranker_active": stable_reranker["active"],
        "reranker_limit": stable_reranker["limit"],
        "embedding_circuit": stable_embedding["circuit"],
        "embedding_active": stable_embedding["active"],
        "embedding_limit": stable_embedding["limit"],
        "pools": pools,
    }
