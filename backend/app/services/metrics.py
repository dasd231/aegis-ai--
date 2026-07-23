from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


CHAT_REQUESTS = Counter(
    "aegis_chat_requests_total",
    "Completed chat requests.",
    ("experiment_group", "retrieval_status", "fallback"),
)
CHAT_RETRIEVAL_SECONDS = Histogram(
    "aegis_chat_retrieval_seconds",
    "Knowledge retrieval latency.",
    ("rag_version", "embedding_version", "vector_index_version"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
CHAT_GENERATION_SECONDS = Histogram(
    "aegis_chat_generation_seconds",
    "Answer generation latency.",
    ("service_version", "model_version"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
CHAT_TTFT_SECONDS = Histogram(
    "aegis_chat_ttft_seconds",
    "Time to first generated token.",
    ("service_version", "model_version"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15),
)
CHAT_TOTAL_SECONDS = Histogram(
    "aegis_chat_total_seconds",
    "End-to-end chat stream processing latency.",
    ("experiment_group",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
CANARY_OBSERVATIONS = Counter(
    "aegis_canary_observations_total",
    "Canary inference observations.",
    ("result", "model_version", "service_version"),
)
CANARY_ENABLED = Gauge(
    "aegis_canary_enabled",
    "Whether the distributed canary kill switch is enabled (1) or disabled (0).",
)


def observe_chat(
    *,
    experiment_group: str,
    retrieval_status: str,
    fallback_used: bool,
    rag_version: str,
    embedding_version: str,
    vector_index_version: str,
    service_version: str,
    model_version: str,
    retrieval_ms: int,
    generation_ms: int,
    total_ms: int,
    ttft_ms: int | None,
) -> None:
    CHAT_REQUESTS.labels(experiment_group, retrieval_status, str(fallback_used).lower()).inc()
    CHAT_RETRIEVAL_SECONDS.labels(
        rag_version,
        embedding_version,
        vector_index_version,
    ).observe(max(0, retrieval_ms) / 1000)
    CHAT_GENERATION_SECONDS.labels(service_version, model_version).observe(max(0, generation_ms) / 1000)
    CHAT_TOTAL_SECONDS.labels(experiment_group).observe(max(0, total_ms) / 1000)
    if ttft_ms is not None:
        CHAT_TTFT_SECONDS.labels(service_version, model_version).observe(max(0, ttft_ms) / 1000)


def observe_canary(*, success: bool, model_version: str, service_version: str) -> None:
    CANARY_OBSERVATIONS.labels("success" if success else "failure", model_version, service_version).inc()
