from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Literal, Protocol


QueryMode = Literal["single_rewrite", "multi_expand"]
RerankPolicy = Literal["disabled", "conditional", "always"]


class RagStrategyConfiguration(Protocol):
    """Settings surface required to materialize immutable RAG strategies."""

    retrieval_top_k: int
    retrieval_final_k: int
    retrieval_rrf_k: int
    retrieval_dense_weight: float
    retrieval_sparse_weight: float
    retrieval_expansion_weight: float
    reranker_candidate_k: int
    reranker_weight: float
    max_context_chars: int
    rag_canary_top_k: int
    rag_canary_final_k: int
    rag_canary_rrf_k: int
    rag_canary_dense_weight: float
    rag_canary_sparse_weight: float
    rag_canary_expansion_weight: float
    rag_canary_candidate_k: int
    rag_canary_rerank_policy: RerankPolicy
    rag_canary_rerank_weight: float
    rag_canary_context_chars: int


class UnknownRagStrategyError(ValueError):
    pass


@dataclass(frozen=True)
class RagStrategy:
    """One reproducible retrieval/rerank/context policy.

    The version is human-facing; ``sha256`` binds the version to every material
    algorithm parameter.  An experiment can therefore distinguish two deploys
    that accidentally reuse a version label with different configuration.
    ``expansion_weight`` is a total budget shared by all expansion channels,
    never a per-expansion multiplier.
    """

    version: str
    query_mode: QueryMode
    top_k: int
    final_k: int
    rrf_k: int
    dense_weight: float
    sparse_weight: float
    expansion_weight: float
    candidate_k: int
    rerank_policy: RerankPolicy
    rerank_weight: float
    context_chars: int
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("RAG strategy version cannot be empty")
        if self.query_mode not in {"single_rewrite", "multi_expand"}:
            raise ValueError("unsupported RAG query mode")
        if self.rerank_policy not in {"disabled", "conditional", "always"}:
            raise ValueError("unsupported RAG rerank policy")
        if not 1 <= self.final_k <= self.candidate_k <= self.top_k <= 50:
            raise ValueError("RAG strategy requires final_k <= candidate_k <= top_k <= 50")
        if not 1 <= self.rrf_k <= 200:
            raise ValueError("RAG strategy rrf_k must be between 1 and 200")
        if min(self.dense_weight, self.sparse_weight, self.expansion_weight) <= 0:
            raise ValueError("RAG strategy channel weights must be positive")
        if not 0 <= self.rerank_weight <= 0.9:
            raise ValueError("RAG strategy rerank_weight must be between 0 and 0.9")
        if not 2_000 <= self.context_chars <= 50_000:
            raise ValueError("RAG strategy context budget must be between 2000 and 50000 characters")

        canonical_payload = {
            "version": self.version,
            "query_mode": self.query_mode,
            "top_k": self.top_k,
            "final_k": self.final_k,
            "rrf_k": self.rrf_k,
            "dense_weight": self.dense_weight,
            "sparse_weight": self.sparse_weight,
            "expansion_weight": self.expansion_weight,
            "candidate_k": self.candidate_k,
            "rerank_policy": self.rerank_policy,
            "rerank_weight": self.rerank_weight,
            "context_chars": self.context_chars,
        }
        canonical = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "sha256", hashlib.sha256(canonical).hexdigest())

    def public_dict(self) -> dict[str, str | int | float]:
        return asdict(self)


class RagStrategyRegistry:
    """Explicit strategy registry; unknown labels never fall back heuristically."""

    _STABLE_VERSIONS = frozenset({"hybrid_v1", "hybrid_v2", "hybrid_rrf_v2"})
    _CANARY_VERSIONS = frozenset({"multi_rrf_v3"})

    @classmethod
    def supported_versions(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._STABLE_VERSIONS | cls._CANARY_VERSIONS))

    @classmethod
    def resolve(cls, version: str, settings: RagStrategyConfiguration) -> RagStrategy:
        if version in cls._STABLE_VERSIONS:
            return RagStrategy(
                version=version,
                query_mode="single_rewrite",
                top_k=settings.retrieval_top_k,
                final_k=settings.retrieval_final_k,
                rrf_k=settings.retrieval_rrf_k,
                dense_weight=settings.retrieval_dense_weight,
                sparse_weight=settings.retrieval_sparse_weight,
                expansion_weight=settings.retrieval_expansion_weight,
                candidate_k=settings.reranker_candidate_k,
                rerank_policy="conditional",
                rerank_weight=settings.reranker_weight,
                context_chars=settings.max_context_chars,
            )
        if version in cls._CANARY_VERSIONS:
            return RagStrategy(
                version=version,
                query_mode="multi_expand",
                top_k=settings.rag_canary_top_k,
                final_k=settings.rag_canary_final_k,
                rrf_k=settings.rag_canary_rrf_k,
                dense_weight=settings.rag_canary_dense_weight,
                sparse_weight=settings.rag_canary_sparse_weight,
                expansion_weight=settings.rag_canary_expansion_weight,
                candidate_k=settings.rag_canary_candidate_k,
                rerank_policy=settings.rag_canary_rerank_policy,
                rerank_weight=settings.rag_canary_rerank_weight,
                context_chars=settings.rag_canary_context_chars,
            )
        supported = ", ".join(cls.supported_versions())
        raise UnknownRagStrategyError(
            f"unknown RAG strategy version {version!r}; supported versions: {supported}"
        )


def get_rag_strategy(version: str, settings: RagStrategyConfiguration) -> RagStrategy:
    return RagStrategyRegistry.resolve(version, settings)
