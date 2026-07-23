from __future__ import annotations

from dataclasses import dataclass
import re

from app.core.config import get_settings
from app.services.rag_strategy import RagStrategy, get_rag_strategy


# Only reviewed, domain-owned aliases are allowed into online query expansion.
# An LLM rewrite may silently change a product, channel, time window, or negation;
# deterministic aliases keep the retrieval experiment reproducible and auditable.
QUERY_EXPANSION_RULES: tuple[tuple[str, str], ...] = (
    (r"(一直|反复|每月).{0,6}(扣钱|扣款)", "自动续费 取消订阅"),
    (r"(电池|手机).{0,6}(变胖|顶起后盖)", "电池鼓包"),
    (r"(黑屏|不开机).{0,10}(没反应|按.*没反应)", "无法开机"),
    (r"(快递|包裹).{0,8}(到哪|咋查|怎么查)", "物流轨迹 查询"),
    (r"(多久能退|多久退)", "退货期限"),
    (r"(多久换新|多久能换)", "换货期限"),
    (r"(质保几年|保修几年|保多久)", "免费维修 保修期限"),
)


@dataclass(frozen=True)
class RetrievalQueryPlan:
    """Versioned retrieval inputs kept separate to avoid embedding dilution."""

    display_query: str
    semantic_queries: tuple[str, ...]
    lexical_query: str
    strategy: str
    expansions: tuple[str, ...]
    rag_version: str
    rag_strategy_sha256: str


def reviewed_expansions(question: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            canonical
            for pattern, canonical in QUERY_EXPANSION_RULES
            if re.search(pattern, question, flags=re.IGNORECASE) and canonical not in question
        )
    )


def rewrite_query(question: str) -> str:
    """Return a human-readable rewrite while preserving the original question."""
    expansions = reviewed_expansions(question)
    if not expansions:
        return question
    return f"{question}\n检索同义词：{'；'.join(expansions)}"


def build_retrieval_plan(
    question: str,
    strategy: RagStrategy | str,
) -> RetrievalQueryPlan:
    """Build stable or canary retrieval inputs without an extra model call.

    Stable uses one semantic vector plus lexical retrieval.  Canary embeds the
    original and each reviewed canonical query independently, then lets the
    vector store fuse their ranked lists.  This makes the RAG gray axis an
    actual algorithm experiment rather than merely a Top-K change.
    """
    resolved = (
        get_rag_strategy(strategy, get_settings())
        if isinstance(strategy, str)
        else strategy
    )
    original = question.strip()
    expansions = reviewed_expansions(original)
    display_query = rewrite_query(original)
    if resolved.query_mode == "multi_expand" and expansions:
        semantic_queries = tuple(dict.fromkeys((original, *expansions)))[:3]
        strategy = "multi_query_weighted_rrf"
    else:
        semantic_queries = (display_query,)
        strategy = "hybrid_weighted_rrf"
    lexical_query = "\n".join(dict.fromkeys((original, *expansions)))
    return RetrievalQueryPlan(
        display_query=display_query,
        semantic_queries=semantic_queries,
        lexical_query=lexical_query,
        strategy=strategy,
        expansions=expansions,
        rag_version=resolved.version,
        rag_strategy_sha256=resolved.sha256,
    )
