from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.database import SessionLocal
from app.models import (
    DocumentStatus,
    KnowledgeBase,
    KnowledgeDocument,
    Message,
    MessageCitation,
    MessageRole,
    ShadowObservation,
)
from app.services.experiment import ExperimentAssignment, stable_assignment
from app.services.canary_control import record_canary_observation
from app.services.claim_verifier import verify_claim_segment
from app.services.intent import suggestions_for
from app.services.knowledge_router import KnowledgeRouteDecision, route_knowledge_base
from app.services.memory import compact_session_memory
from app.services.metrics import observe_chat
from app.services.prompt_registry import get_prompt_registry
from app.services.provider_credentials import LLM_STABLE, current_runtime_provider
from app.services.query_planner import build_retrieval_plan, rewrite_query
from app.services.rag_strategy import RagStrategy, get_rag_strategy
from app.services.reranker import get_evidence_reranker
from app.services.resilience import llm_breaker, llm_bulkhead
from app.services.vector_store import SearchHit, get_vector_store
from app.services.user_profile import apply_profile_retrieval_hints


logger = logging.getLogger(__name__)


# A semantic embedding can rank two different products close merely because
# both questions use words such as “价格、售后、小米”.  Product and service
# facets are therefore treated as hard business entities before evidence is
# allowed into the generation prompt.  This is deliberately deterministic:
# rerankers improve ordering, but must never be the only hallucination gate.
PRODUCT_FACETS: dict[str, tuple[str, ...]] = {
    "phone": ("手机", "红米", "redmi", "xiaomi 1", "澎湃os"),
    "car": ("汽车", "小米su7", "su7", "小米yu7", "yu7"),
    "television": ("电视", "电视机", "redmi tv", "xiaomi tv"),
    "tablet": ("平板", "pad"),
    "laptop": ("笔记本", "电脑", "book pro"),
    "wearable": ("手环", "手表", "watch", "band"),
    "router": ("路由器",),
    "appliance": ("空调", "冰箱", "洗衣机", "净水器", "扫地机器人"),
}

SERVICE_FACETS: dict[str, tuple[str, ...]] = {
    "order": ("订单", "下单", "订单号"),
    "logistics": ("物流", "快递", "发货", "收货"),
    "refund": ("退款", "退货", "退换", "退订"),
    "invoice": ("发票", "开票"),
    "account": ("账号", "账户", "登录", "密码"),
    "renewal": ("自动续费", "续费", "订阅"),
    "installation": ("上门安装", "安装费", "安装支架", "挂架"),
    "insurance": ("保险", "车险", "商业险"),
    "warranty": ("保修", "质保", "保修期"),
    "repair": ("维修", "检修", "售后网点"),
    "payment": ("支付", "扣款", "付款"),
    "network_settings": ("网络设置", "重置网络", "wi-fi", "wifi", "蓝牙配对"),
    "system_update": ("系统更新", "系统升级", "正式更新", "更新前"),
}

# Domains that are semantically close to customer-service text but belong to a
# different product ontology.  A generic embedding often maps “坐席/企业版/单
# 点登录” near account/order help even when this Xiaomi corpus contains no
# contact-center SaaS.  These precise markers are a hard incompatibility gate.
DOMAIN_FACETS: dict[str, tuple[str, ...]] = {
    "contact_center_saas": ("坐席", "单点登录", "sso", "企业版", "标准版"),
}


def _matched_facets(text: str, definitions: dict[str, tuple[str, ...]]) -> set[str]:
    normalized = re.sub(r"\s+", " ", text.lower())
    return {
        facet
        for facet, aliases in definitions.items()
        if any(alias in normalized for alias in aliases)
    }


def _entity_compatible(question: str, hit: SearchHit) -> bool:
    """Reject cross-product semantic matches while retaining generic policies.

    A product-less chunk can still answer a product-qualified question when it
    matches the same generic business operation (for example “手机订单物流” to
    a store-wide logistics policy).  Product-specific facts such as car
    insurance or television installation cannot fall through to an unrelated
    phone document.
    """
    searchable = "\n".join(
        (
            hit.source_name,
            hit.section_path,
            hit.content,
            hit.parent_content,
        )
    )
    query_domains = _matched_facets(question, DOMAIN_FACETS)
    evidence_domains = _matched_facets(searchable, DOMAIN_FACETS)
    if query_domains and not query_domains.intersection(evidence_domains):
        return False

    query_products = _matched_facets(question, PRODUCT_FACETS)
    if not query_products:
        return True
    evidence_products = _matched_facets(searchable, PRODUCT_FACETS)
    if query_products & evidence_products:
        return True
    # A chunk explicitly about another product is never compatible.
    if evidence_products:
        return False
    query_services = _matched_facets(question, SERVICE_FACETS)
    evidence_services = _matched_facets(searchable, SERVICE_FACETS)
    return bool(query_services & evidence_services)

@dataclass(frozen=True)
class RankedEvidence:
    hit: SearchHit
    score: float
    lexical_score: float
    source_id: str


@dataclass
class GroundingState:
    removed_segments: int = 0
    cited_segments: int = 0


@dataclass(frozen=True)
class RerankTrace:
    attempted: bool = False
    applied: bool = False
    reason: str = "disabled"


@dataclass
class GenerationTrace:
    output_tokens: int | None = None
    finish_reason: str | None = None


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    # CJK single characters create dangerous false positives (for example,
    # unrelated questions often share “的、是、期、年”).  Bigrams retain
    # domain phrases such as “退款、发票、单点登录”; a unigram is used only
    # when the entire CJK run itself contains one character.
    chars = {run for run in runs if len(run) == 1}
    bigrams = {run[index : index + 2] for run in runs for index in range(len(run) - 1)}
    terms = words | chars | bigrams
    # These terms appear in nearly every tenant document or in common prompt
    # injection wording, so they carry no domain-discriminative value. Keeping
    # them would make unsupported product questions look grounded merely because
    # they mention a brand, the knowledge base, or generic "rules".
    terms.difference_update({"小米", "知识", "识库", "资料", "规则", "客服", "演示"})
    return terms


def contextualize_retrieval_query(
    question: str,
    history: Sequence[tuple[str, str]],
    *,
    max_chars: int = 800,
) -> str:
    """Attach the latest user turn only for genuinely context-dependent follow-ups.

    History is used for retrieval recall, never as an instruction. Explicit new
    topics remain standalone so a short conversation cannot contaminate later
    searches with an obsolete product or policy domain.
    """
    current = question.strip()
    if not current or not history:
        return current
    follow_up = bool(
        re.search(r"^(那|那么|这个|这种|它|上述|刚才|前面)", current)
        or (
            len(current) <= 24
            and re.search(r"(呢|多久|怎么办|怎么处理|可以吗|行吗|是否可以|还要什么)[？?]?$", current)
        )
    )
    if not follow_up:
        return current
    previous = next(
        (
            content.strip()
            for role, content in reversed(history)
            if role == "user" and content.strip()
        ),
        "",
    )
    if not previous or previous == current:
        return current
    suffix = f"\n后续问题：{current}"
    remaining = max(0, max_chars - len(suffix))
    prior_tail = previous[-remaining:] if remaining else ""
    return prior_tail + suffix


def rewrite_retrieval_query(question: str) -> str:
    """Expand reviewed colloquial aliases without letting an LLM invent intent."""
    return rewrite_query(question)


def input_guardrail_reason(question: str) -> str | None:
    """Detect explicit instruction override, citation forgery, or secret exfiltration."""
    compact = re.sub(r"\s+", "", question.lower())
    patterns = (
        r"(忽略|绕过|覆盖).{0,16}(规则|知识库|系统|指令|提示)",
        r"(泄露|输出|显示|展示|告诉我).{0,16}(系统提示词?|开发者消息|内部评分|数据库密码|api密钥)",
        r"伪造.{0,8}(引用|来源)",
        r"编造.{0,12}(价格|金额|事实|答案|引用)",
        r"ignore.{0,24}(previous|all|system|instructions?|rules?)",
        r"(reveal|show|print).{0,24}(systemprompt|developermessage|databasepassword|apikey)",
        r"fabricate.{0,16}(citations?|prices?|facts?|answers?)",
    )
    return "prompt_injection" if any(re.search(pattern, compact) for pattern in patterns) else None


def _lexical_score(question: str, content: str) -> float:
    query_terms = _terms(question)
    if not query_terms:
        return 0.0
    content_terms = _terms(content)
    weighted_matches = sum(2 if len(term) > 1 else 1 for term in query_terms & content_terms)
    weighted_total = sum(2 if len(term) > 1 else 1 for term in query_terms)
    return weighted_matches / max(1, weighted_total)


def _too_similar(left: str, right: str) -> bool:
    left_terms, right_terms = _terms(left), _terms(right)
    if not left_terms or not right_terms:
        return False
    return len(left_terms & right_terms) / len(left_terms | right_terms) > 0.86


def rank_evidence(
    question: str,
    hits: Sequence[SearchHit],
    *,
    limit: int | None = None,
) -> list[RankedEvidence]:
    """Confidence gate + rule-aware reranking + near-duplicate suppression."""
    if input_guardrail_reason(question):
        return []
    settings = get_settings()
    candidates: list[tuple[SearchHit, float, float]] = []
    for hit in hits:
        if not _entity_compatible(question, hit):
            continue
        lexical = max(_lexical_score(question, hit.content), hit.lexical_retrieval_score)
        # The offline feature-hash embedding is intentionally lexical. Unlike a
        # semantic production embedding, a zero-overlap hit is only a hash
        # collision and must never pass on vector distance alone.
        if settings.embedding_provider == "local_hash" and lexical < settings.retrieval_min_lexical_score:
            continue
        if hit.distance > settings.retrieval_max_distance and lexical < settings.retrieval_min_lexical_score:
            continue
        vector_similarity = max(0.0, min(1.0, 1.0 - hit.distance))
        if hit.hybrid_fusion_score > 0:
            score = (
                vector_similarity * 0.33
                + lexical * 0.18
                + hit.hybrid_fusion_score * 0.46
                + min(hit.priority, 3) * 0.01
            )
        else:
            score = vector_similarity * 0.68 + lexical * 0.25 + min(hit.priority, 3) * 0.01
        candidates.append((hit, score, lexical))
    candidates.sort(key=lambda item: (item[1], item[0].priority), reverse=True)

    target_limit = limit if limit is not None else settings.retrieval_final_k
    selected: list[tuple[SearchHit, float, float]] = []
    # Guarantee that a strongly relevant critical rule is not buried by many generic chunks.
    best_score = candidates[0][1] if candidates else 0.0
    critical = [
        item
        for item in candidates
        if item[0].priority > 0 and item[1] >= best_score - settings.retrieval_critical_margin
    ]
    if critical:
        selected.append(critical[0])
    for candidate in candidates:
        if candidate in selected:
            continue
        candidate_parent = (candidate[0].document_id, candidate[0].parent_id)
        if candidate[0].parent_id and any(
            (prior[0].document_id, prior[0].parent_id) == candidate_parent for prior in selected
        ):
            continue
        if any(_too_similar(candidate[0].content, prior[0].content) for prior in selected):
            continue
        selected.append(candidate)
        if len(selected) >= target_limit:
            break
    selected.sort(key=lambda item: (item[1], item[0].priority), reverse=True)
    return [
        RankedEvidence(hit=hit, score=score, lexical_score=lexical, source_id=f"S{index}")
        for index, (hit, score, lexical) in enumerate(selected, start=1)
    ]


def should_rerank(
    question: str,
    evidence: Sequence[RankedEvidence],
    *,
    min_candidates: int,
    ambiguity_margin: float,
) -> bool:
    if len(evidence) < min_candidates:
        return False
    compound_markers = (
        "分别",
        "同时",
        "以及",
        "并且",
        "区别",
        "比较",
        "哪些条件",
        "所有条件",
        "各自",
        "先",
        "然后",
    )
    if any(marker in question for marker in compound_markers):
        return True
    scores = sorted((item.score for item in evidence), reverse=True)
    return len(scores) >= 2 and scores[0] - scores[1] <= ambiguity_margin


def apply_reranker_scores(
    evidence: Sequence[RankedEvidence],
    scores: dict[str, float],
    *,
    weight: float,
    final_k: int,
) -> list[RankedEvidence]:
    updated = [
        replace(
            item,
            score=round((1 - weight) * item.score + weight * scores[item.hit.chunk_id], 6),
        )
        for item in evidence
        if item.hit.chunk_id in scores
    ]
    updated.sort(key=lambda item: (item.score, item.hit.priority), reverse=True)
    return [replace(item, source_id=f"S{index}") for index, item in enumerate(updated[:final_k], 1)]


async def maybe_rerank(
    question: str,
    evidence: Sequence[RankedEvidence],
    *,
    final_k: int | None = None,
    force: bool = False,
    strategy: RagStrategy | None = None,
) -> tuple[list[RankedEvidence], RerankTrace]:
    settings = get_settings()
    strategy = strategy or get_rag_strategy(settings.rag_stable_version, settings)
    final_k = final_k or strategy.final_k
    deterministic = [
        replace(item, source_id=f"S{index}")
        for index, item in enumerate(evidence[:final_k], 1)
    ]
    if not settings.reranker_enabled:
        return deterministic, RerankTrace(reason="disabled")
    if strategy.rerank_policy == "disabled" and not force:
        return deterministic, RerankTrace(reason="strategy_disabled")
    if settings.reranker_provider == "llm_json" and settings.llm_provider != "openai_compatible":
        return deterministic, RerankTrace(reason="disabled")
    always_rerank = force or strategy.rerank_policy == "always"
    if not always_rerank and not should_rerank(
        question,
        evidence,
        min_candidates=settings.reranker_min_candidates,
        ambiguity_margin=settings.reranker_ambiguity_margin,
    ):
        return deterministic, RerankTrace(reason="confident_deterministic_rank")

    documents = [
        {
            "id": item.hit.chunk_id,
            "section": item.hit.section_path,
            "text": item.hit.content[:1600],
        }
        for item in evidence
    ]
    scores = await get_evidence_reranker().score(question, documents)
    if scores is None:
        return deterministic, RerankTrace(attempted=True, reason="dependency_fallback")
    return (
        apply_reranker_scores(
            evidence,
            scores,
            weight=strategy.rerank_weight,
            final_k=final_k,
        ),
        RerankTrace(
            attempted=True,
            applied=True,
            reason=(
                "forced_rerank"
                if force
                else "strategy_always"
                if strategy.rerank_policy == "always"
                else "ambiguous_or_compound_query"
            ),
        ),
    )


def build_context(
    evidence: Sequence[RankedEvidence],
    strategy: RagStrategy | None = None,
) -> str:
    settings = get_settings()
    strategy = strategy or get_rag_strategy(settings.rag_stable_version, settings)
    remaining = strategy.context_chars
    blocks: list[str] = []
    for item in evidence:
        kind = "关键规则" if item.hit.priority else "参考资料"
        section = f"；章节={item.hit.section_path}" if item.hit.section_path else ""
        header = f"[{item.source_id}] 类型={kind}；文档={item.hit.source_name}{section}\n"
        allowance = remaining - len(header) - 20
        if allowance < 160:
            break
        # Child chunks maximize retrieval precision; the bounded parent restores
        # nearby conditions and exceptions needed for a complete answer.
        source_content = item.hit.parent_content or item.hit.content
        if len(source_content) <= allowance:
            content = source_content
        else:
            # Prefix truncation can drop the exact child passage that won
            # retrieval when it lives near the end of a long manual section.
            child_start = item.hit.child_start
            child_end = item.hit.child_end
            if not (0 <= child_start < child_end <= len(source_content)):
                child_start = source_content.find(item.hit.content)
                child_end = child_start + len(item.hit.content)
            if 0 <= child_start < child_end <= len(source_content):
                child_length = child_end - child_start
                if child_length >= allowance:
                    content = source_content[child_start : child_start + allowance]
                else:
                    surrounding = allowance - child_length
                    window_start = max(0, child_start - surrounding // 2)
                    window_end = min(len(source_content), window_start + allowance)
                    window_start = max(0, window_end - allowance)
                    content = source_content[window_start:window_end]
                    if window_start:
                        content = "…" + content[1:]
                    if window_end < len(source_content):
                        content = content[:-1] + "…"
            else:
                content = source_content[:allowance]
        block = f"{header}{content}\n[/{item.source_id}]"
        blocks.append(block)
        remaining -= len(block)
    return "\n\n".join(blocks)


def build_messages(
    question: str,
    history: Sequence[tuple[str, str]],
    evidence: Sequence[RankedEvidence],
    profile_context: str = "",
    long_memory: str = "",
    task_context: str = "",
    prompt_version: str | None = None,
    rag_strategy: RagStrategy | None = None,
) -> list[dict[str, str]]:
    context = build_context(evidence, rag_strategy)
    selected_prompt = prompt_version or get_settings().prompt_stable_version
    system_prompt = get_prompt_registry().get(selected_prompt).content
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    remaining_history = get_settings().max_history_chars
    bounded_history: list[tuple[str, str]] = []
    for role, content in reversed(history):
        if remaining_history <= 0:
            break
        clipped = content[-remaining_history:]
        if len(clipped) < len(content):
            clipped = "[历史内容已截断]" + clipped
        bounded_history.append((role, clipped))
        remaining_history -= len(clipped)
    for role, content in reversed(bounded_history):
        if role in {"user", "assistant"}:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": (
                f"<profile_preferences>\n{profile_context}\n</profile_preferences>\n\n"
                f"<long_term_memory>\n{long_memory}\n</long_term_memory>\n\n"
                f"<active_tasks>\n{task_context}\n</active_tasks>\n\n"
                f"<evidence>\n{context}\n</evidence>\n\n<question>\n{question}\n</question>"
            ),
        }
    )
    return messages


class AnswerGenerator:
    def __init__(self, service_pool: str = "stable"):
        self.settings = get_settings()
        self.service_pool = service_pool
        self.client: AsyncOpenAI | None = None
        self._clients: dict[tuple, AsyncOpenAI] = {}
        if self.settings.llm_provider == "openai_compatible":
            canary = service_pool == "canary"
            api_key = (
                self.settings.llm_canary_api_key.get_secret_value()
                if canary
                else self.settings.llm_api_key.get_secret_value()
            ) or "ollama-local"
            base_url = self.settings.llm_canary_base_url if canary else self.settings.llm_base_url
            if canary and not base_url:
                raise RuntimeError("canary inference pool selected without LLM_CANARY_BASE_URL")
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=0,
            )
            self._clients[("environment", service_pool, base_url)] = self.client

    def _request_client(self) -> tuple[AsyncOpenAI | None, str]:
        """Resolve the encrypted DB credential per request for cross-Pod updates.

        Clients are version-keyed and retained until shutdown so an operator
        rotation cannot close a socket that an in-flight stream still uses.
        """
        if self.service_pool != "stable":
            return self.client, self.settings.llm_canary_model or self.settings.llm_model
        configured = current_runtime_provider(LLM_STABLE, require_enabled=False)
        if configured is None:
            return self.client, self.settings.llm_model
        if not configured.enabled:
            return None, configured.model
        identity = (
            "database",
            configured.id,
            configured.version,
            configured.base_url,
            configured.model,
        )
        client = self._clients.get(identity)
        if client is None:
            client = AsyncOpenAI(
                api_key=configured.api_key,
                base_url=configured.base_url,
                timeout=self.settings.llm_timeout_seconds,
                max_retries=0,
            )
            self._clients[identity] = client
        return client, configured.model

    async def stream(
        self,
        question: str,
        history: Sequence[tuple[str, str]],
        evidence: Sequence[RankedEvidence],
        profile_context: str = "",
        long_memory: str = "",
        task_context: str = "",
        *,
        prompt_version: str | None = None,
        model_version: str | None = None,
        rag_strategy: RagStrategy | None = None,
        trace: GenerationTrace | None = None,
    ) -> AsyncIterator[str]:
        client, configured_model = self._request_client()
        if client is None:
            source_lines = [
                f"- {text} [{source_id}]"
                for text, source_id in _relevant_extracts(question, evidence)
            ]
            answer = "根据知识库，相关信息如下：\n\n" + "\n".join(source_lines)
            answer += "\n\n以上内容仅依据已检索到的资料；如需更具体结论，请补充问题细节。"
            if trace is not None:
                trace.finish_reason = "stop"
            for index in range(0, len(answer), 4):
                await asyncio.sleep(0)
                yield answer[index : index + 4]
            return

        messages = build_messages(
            question,
            history,
            evidence,
            profile_context,
            long_memory,
            task_context,
            prompt_version,
            rag_strategy,
        )
        retryable = (RateLimitError, APITimeoutError, APIConnectionError)
        breaker = llm_breaker()
        async with llm_bulkhead().slot():
            circuit_call = breaker.before_call()
            try:
                stream = None
                for attempt in range(self.settings.llm_max_retries + 1):
                    try:
                        stream = await client.chat.completions.create(
                            model=model_version or configured_model,
                            messages=messages,
                            temperature=0.1,
                            max_completion_tokens=self.settings.llm_max_output_tokens,
                            stream=True,
                            stream_options={"include_usage": True},
                        )
                        break
                    except retryable:
                        if attempt >= self.settings.llm_max_retries:
                            raise
                        await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                if stream is None:
                    raise RuntimeError("LLM stream was not created")
                async for chunk in stream:
                    if trace is not None and chunk.usage is not None:
                        trace.output_tokens = chunk.usage.completion_tokens
                    if trace is not None and chunk.choices and chunk.choices[0].finish_reason:
                        trace.finish_reason = chunk.choices[0].finish_reason
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        yield delta
                breaker.succeeded(circuit_call)
            except asyncio.CancelledError:
                breaker.abandoned(circuit_call)
                raise
            except Exception:
                breaker.failed(circuit_call)
                raise

    async def close(self) -> None:
        clients = list({id(client): client for client in self._clients.values()}.values())
        self._clients.clear()
        self.client = None
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


_answer_generators: dict[str, AnswerGenerator] = {}
_shadow_tasks: set[asyncio.Task] = set()


def get_answer_generator(service_pool: str = "stable") -> AnswerGenerator:
    if service_pool not in _answer_generators:
        _answer_generators[service_pool] = AnswerGenerator(service_pool)
    return _answer_generators[service_pool]


async def close_answer_generator() -> None:
    tasks = list(_shadow_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    generators = list(_answer_generators.values())
    _answer_generators.clear()
    await asyncio.gather(*(generator.close() for generator in generators))


def _should_shadow(experiment: ExperimentAssignment, intent: str) -> bool:
    settings = get_settings()
    return bool(
        settings.llm_provider == "openai_compatible"
        and experiment.shadow_percent > 0
        and experiment.group == "stable"
        and intent not in settings.stable_intent_set
        and experiment.buckets.get("shadow", 10_000) < experiment.shadow_percent * 100
    )


async def _run_shadow_comparison(
    *,
    stable_message_id: str,
    question: str,
    retrieval_question: str,
    knowledge_base_id: str | None,
    stable_answer: str,
    buckets: dict[str, int],
) -> None:
    """Run a side-effect-free canary pipeline without delaying the user stream."""
    settings = get_settings()
    prompt = get_prompt_registry().get(settings.prompt_canary_version)
    assignment = ExperimentAssignment(
        group="shadow",
        model_pool="canary",
        model_version=settings.llm_canary_model,
        embedding_pool="canary" if settings.qdrant_canary_collection else "stable",
        embedding_version=(
            settings.embedding_canary_version
            if settings.qdrant_canary_collection
            else settings.embedding_stable_version
        ),
        embedding_model=(
            settings.embedding_canary_model
            if settings.qdrant_canary_collection
            else settings.embedding_model
        ),
        embedding_dimensions=(
            settings.embedding_canary_dimensions
            if settings.qdrant_canary_collection
            else settings.embedding_dimensions
        ),
        vector_index_version=(
            settings.qdrant_canary_collection or settings.qdrant_collection
        ),
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
        rag_version=settings.rag_canary_version,
        service_pool="canary",
        service_version=settings.llm_service_canary_version,
        buckets=buckets,
        reason="shadow_sample",
        shadow_percent=0,
    )
    rag_strategy = get_rag_strategy(assignment.rag_version, settings)
    started = time.perf_counter()
    candidate = ""
    status = "completed"
    error_code: str | None = None
    citation_valid = False
    try:
        async with asyncio.timeout(settings.shadow_timeout_seconds):
            top_k, final_k = rag_strategy.top_k, rag_strategy.final_k
            shadow_plan = build_retrieval_plan(retrieval_question, rag_strategy)
            route = await asyncio.to_thread(
                route_knowledge_base,
                shadow_plan.display_query,
                knowledge_base_id,
            )
            hits = await get_vector_store(assignment.embedding_pool).query_many(
                shadow_plan.semantic_queries,
                top_k,
                route.knowledge_base_id,
                lexical_query=shadow_plan.lexical_query,
                strategy=rag_strategy,
            )
            hits = await asyncio.to_thread(_only_ready_hits, hits)
            candidate_limit = (
                rag_strategy.candidate_k
                if settings.reranker_enabled and rag_strategy.rerank_policy != "disabled"
                else final_k
            )
            evidence = rank_evidence(shadow_plan.display_query, hits, limit=candidate_limit)
            evidence, _ = await maybe_rerank(
                shadow_plan.display_query,
                evidence,
                final_k=final_k,
                strategy=rag_strategy,
            )
            if not evidence:
                status = "empty_retrieval"
            else:
                stream = get_answer_generator("canary").stream(
                    question,
                    [],
                    evidence,
                    prompt_version=assignment.prompt_version,
                    model_version=assignment.model_version,
                    rag_strategy=rag_strategy,
                )
                chunks = [chunk async for chunk in stream]
                candidate = "".join(chunks).strip()
                citation_valid = _valid_citations(candidate, evidence)
                if not citation_valid:
                    status = "invalid_citation"
    except Exception as exc:
        logger.exception("shadow canary pipeline failed")
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        error_code = type(exc).__name__[:80]

    stable_terms = _terms(stable_answer)
    candidate_terms = _terms(candidate)
    overlap = None
    if stable_terms or candidate_terms:
        overlap = round(
            len(stable_terms & candidate_terms) / max(1, len(stable_terms | candidate_terms)) * 10_000
        )
    with SessionLocal() as db:
        db.add(
            ShadowObservation(
                stable_message_id=stable_message_id,
                model_version=assignment.model_version,
                embedding_version=assignment.embedding_version,
                embedding_model=assignment.embedding_model,
                vector_index_version=assignment.vector_index_version,
                prompt_version=assignment.prompt_version,
                prompt_sha256=assignment.prompt_sha256,
                rag_version=assignment.rag_version,
                service_version=assignment.service_version,
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000),
                stable_answer_sha256=hashlib.sha256(stable_answer.encode("utf-8")).hexdigest(),
                canary_answer_sha256=(
                    hashlib.sha256(candidate.encode("utf-8")).hexdigest() if candidate else None
                ),
                canary_answer_chars=len(candidate),
                lexical_overlap=overlap,
                citation_valid=citation_valid,
                side_effects_mocked=True,
                error_code=error_code,
            )
        )
        db.commit()


def _schedule_shadow(**kwargs) -> None:
    task = asyncio.create_task(_run_shadow_comparison(**kwargs))
    _shadow_tasks.add(task)
    task.add_done_callback(_shadow_tasks.discard)


def _valid_citations(answer: str, evidence: Sequence[RankedEvidence]) -> bool:
    cited_segments: list[str] = []
    for line in answer.splitlines():
        cited_segments.extend(
            segment
            for segment in re.findall(r".*?[。！？](?:\s*\[S\d+\])*|.+$", line)
            if re.search(r"\[S\d+\]", segment)
        )
    return bool(cited_segments) and all(
        verify_claim_segment(segment, evidence).supported for segment in cited_segments
    )


def _segment_is_safe(segment: str, allowed: set[str]) -> bool:
    stripped = re.sub(r"^[\s>*#\-\d.]+", "", segment).strip()
    if not stripped:
        return True
    mentioned = set(re.findall(r"\[(S\d+)\]", segment))
    if mentioned:
        return mentioned.issubset(allowed)
    if stripped.endswith(("：", ":")):
        return True
    if len(stripped) <= 10 and not re.search(r"[。！？\d]", stripped):
        return True
    safe_phrases = ("知识库未提供", "无法确认", "无法回答", "请联系人工", "仅依据", "请补充")
    return any(phrase in stripped for phrase in safe_phrases)


def _segment_is_grounded(segment: str, evidence: Sequence[RankedEvidence]) -> bool:
    """Require semantic support for cited factual segments, not only a valid ID."""
    allowed = {item.source_id for item in evidence}
    if not _segment_is_safe(segment, allowed):
        return False
    if not re.search(r"\[S\d+\]", segment):
        return True
    return verify_claim_segment(segment, evidence).supported


def _take_complete_grounding_segment(buffer: str) -> tuple[str, str] | None:
    """Return one complete line/sentence while preserving a trailing citation suffix."""
    for index, character in enumerate(buffer):
        if character == "\n":
            return buffer[: index + 1], buffer[index + 1 :]
        if character not in "。！？":
            continue
        cursor = index + 1
        while cursor < len(buffer) and buffer[cursor] in " \t\r":
            cursor += 1
        if cursor == len(buffer):
            return None
        while buffer[cursor] == "[":
            citation = re.match(r"\[S\d+\]", buffer[cursor:])
            if citation is None:
                return None  # A citation token can itself be split across chunks.
            cursor += citation.end()
            while cursor < len(buffer) and buffer[cursor] in " \t\r":
                cursor += 1
            if cursor == len(buffer):
                return None
        if buffer[cursor] == "\n":
            cursor += 1
        return buffer[:cursor], buffer[cursor:]
    return None


async def _grounded_deltas(
    source: AsyncIterator[str], evidence: Sequence[RankedEvidence], state: GroundingState
) -> AsyncIterator[str]:
    pending = ""
    async for delta in source:
        pending += delta
        while True:
            completed = _take_complete_grounding_segment(pending)
            if completed is None:
                break
            segment, pending = completed
            if _segment_is_grounded(segment, evidence):
                if re.search(r"\[S\d+\]", segment):
                    state.cited_segments += 1
                yield segment
            else:
                state.removed_segments += 1
    if pending:
        if _segment_is_grounded(pending, evidence):
            if re.search(r"\[S\d+\]", pending):
                state.cited_segments += 1
            yield pending
        else:
            state.removed_segments += 1


def _relevant_extracts(
    question: str,
    evidence: Sequence[RankedEvidence],
    *,
    max_items: int = 4,
) -> list[tuple[str, str]]:
    """Select query-relevant source sentences for the offline/safe fallback path."""
    raw_candidates: list[tuple[RankedEvidence, int, str, set[str]]] = []
    seen_sentences: set[tuple[str, str]] = set()
    for item in evidence:
        content = item.hit.parent_content or item.hit.content
        sentences = re.findall(r"[^。！？\n]+[。！？]?", content)
        for position, raw_sentence in enumerate(sentences):
            sentence = raw_sentence.strip(" \t\r\n#*-_")
            if len(sentence) < 4 or sentence.endswith(("？", "?")):
                continue
            dedupe_key = (item.hit.document_id, re.sub(r"\s+", "", sentence))
            if dedupe_key in seen_sentences:
                continue
            seen_sentences.add(dedupe_key)
            raw_candidates.append((item, position, sentence[:500], _terms(sentence)))

    query_terms = _terms(question)
    if not raw_candidates or not query_terms:
        return [(item.hit.summary[:500], item.source_id) for item in evidence[:1]]
    document_frequency = {
        term: sum(term in sentence_terms for _, _, _, sentence_terms in raw_candidates)
        for term in query_terms
    }
    term_weights = {
        term: math.log((len(raw_candidates) + 1) / (frequency + 1)) + 1
        for term, frequency in document_frequency.items()
    }
    total_weight = sum(term_weights.values()) or 1.0
    candidates: list[tuple[float, float, int, str, RankedEvidence, set[str]]] = []
    for item, position, sentence, sentence_terms in raw_candidates:
        lexical = sum(term_weights[term] for term in query_terms & sentence_terms) / total_weight
        if lexical <= 0:
            continue
        # IDF suppresses generic words such as “需要/提供”; evidence rank and
        # source position only break otherwise close, fully deterministic ties.
        score = lexical * 0.85 + item.score * 0.15
        candidates.append((score, lexical, -position, sentence, item, sentence_terms))
    candidates.sort(key=lambda candidate: candidate[:3], reverse=True)
    if not candidates:
        return [(item.hit.summary[:500], item.source_id) for item in evidence[:1]]

    facets = [part.strip() for part in re.split(r"[，,；;。！？?]", question) if part.strip()]
    aspect_facets: list[str] = []
    for pattern in (
        r"在(.+?)(?:和|与|以及)(.+?)(?:方面|上)",
        r"(.{2,16}?)(?:和|与|以及)(.{2,16}?)分别",
    ):
        for match in re.finditer(pattern, question):
            matched_facets = [part.strip() for part in match.groups() if part.strip()]
            facets.extend(matched_facets)
            aspect_facets.extend(matched_facets)
    facets = list(dict.fromkeys(facets))

    def facet_pool(
        pool: list[tuple[float, float, int, str, RankedEvidence, set[str]]],
        facet: str,
    ) -> list[tuple[float, float, int, str, RankedEvidence, set[str]]]:
        filtered = pool
        if re.search(r"多少|多久|几次|多长|几点", facet):
            quantified = [
                candidate
                for candidate in filtered
                if re.search(r"\d|[一二三四五六七八九十百]", candidate[3])
            ]
            if quantified:
                filtered = quantified
        if re.search(r"禁止|不得|不能|不允许|敏感|吗", facet):
            prohibited = [
                candidate
                for candidate in filtered
                if re.search(r"禁止|不得|不能|不予|不可|不包含|仅", candidate[3])
            ]
            if prohibited:
                filtered = prohibited
        if re.search(r"哪些|哪几|资料|材料", facet):
            enumerations = [candidate for candidate in filtered if re.search(r"、|以及|和", candidate[3])]
            if enumerations:
                filtered = enumerations
        return filtered

    def facet_rank(
        candidate: tuple[float, float, int, str, RankedEvidence, set[str]],
        facet: str,
    ) -> tuple[float, float, int]:
        facet_terms = _terms(facet) & query_terms
        if not facet_terms:
            return 0.0, candidate[0], candidate[2]
        facet_weight = sum(term_weights.get(term, 1.0) for term in facet_terms) or 1.0
        coverage = sum(
            term_weights.get(term, 1.0) for term in facet_terms & candidate[5]
        ) / facet_weight
        return coverage, candidate[0], candidate[2]

    # Prefer the one document that jointly covers all query facets over mixing
    # superficially similar policy domains. Real model mode still receives the
    # complete bounded evidence set.
    document_ids = list(dict.fromkeys(candidate[4].hit.document_id for candidate in candidates))

    def document_rank(document_id: str) -> tuple[float, float]:
        pool = [candidate for candidate in candidates if candidate[4].hit.document_id == document_id]
        facet_coverage = 0.0
        for facet in facets:
            scoped = facet_pool(pool, facet)
            facet_coverage += max(
                (facet_rank(candidate, facet)[0] for candidate in scoped), default=0.0
            )
        return facet_coverage, max(candidate[0] for candidate in pool)

    primary_document_id = evidence[0].hit.document_id
    if primary_document_id not in document_ids:
        # Semantic retrieval can surface a top source with no literal query
        # overlap while another source contributes the extractive fallback.
        # In that case candidates is non-empty but the original top document
        # has no eligible sentence; use the best actual candidate rather than
        # indexing an empty per-document list.
        primary_document_id = candidates[0][4].hit.document_id
    if aspect_facets:
        original_facets = facets
        facets = aspect_facets
        primary_document_id = max(document_ids, key=document_rank)
        facets = original_facets
    document_candidates = [
        candidate for candidate in candidates if candidate[4].hit.document_id == primary_document_id
    ]
    selected_candidates: list[tuple[float, float, int, str, RankedEvidence, set[str]]] = []

    def add(candidate: tuple[float, float, int, str, RankedEvidence, set[str]]) -> None:
        if candidate not in selected_candidates and len(selected_candidates) < max_items:
            selected_candidates.append(candidate)

    add(document_candidates[0])
    for facet in facets:
        pool = facet_pool(document_candidates, facet)
        best = max(pool, key=lambda candidate: facet_rank(candidate, facet))
        if facet_rank(best, facet)[0] > 0:
            add(best)
    # A single natural-language facet may still require several procedural
    # steps. Fill the remaining budget with ranked sentences from the same
    # primary document so the deterministic fallback is useful, while avoiding
    # accidental policy mixing across documents.
    for candidate in document_candidates:
        add(candidate)

    return [(candidate[3], candidate[4].source_id) for candidate in selected_candidates]


def _offline_evidence_subset(
    question: str, evidence: Sequence[RankedEvidence]
) -> list[RankedEvidence]:
    """Keep source cards aligned with the sentences the offline demo will use."""
    used_ids = {source_id for _, source_id in _relevant_extracts(question, evidence)}
    selected = [item for item in evidence if item.source_id in used_ids]
    return [replace(item, source_id=f"S{index}") for index, item in enumerate(selected, 1)]


def _extractive_answer(
    question: str,
    evidence: Sequence[RankedEvidence],
    *,
    degraded: bool = False,
) -> str:
    prefix = "生成模型暂时不可用，以下为知识片段的安全摘录：" if degraded else "根据知识库，可确认："
    lines = [f"- {text} [{source_id}]" for text, source_id in _relevant_extracts(question, evidence)]
    return prefix + "\n\n" + "\n".join(lines)


def _structured_claims(answer: str, evidence: Sequence[RankedEvidence]) -> list[dict[str, object]]:
    """Expose only claims that carry valid evidence IDs; never expose model reasoning."""
    allowed = {item.source_id for item in evidence}
    claims: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    segments: list[str] = []
    for line in answer.splitlines():
        segments.extend(re.findall(r".*?[。！？](?:\s*\[S\d+\])*|.+$", line))
    for segment in segments:
        evidence_ids = tuple(sorted(set(re.findall(r"\[(S\d+)\]", segment))))
        if not evidence_ids or not set(evidence_ids).issubset(allowed):
            continue
        text = re.sub(r"\s*\[S\d+\]", "", segment).strip(" \n-*>")
        if not text:
            continue
        verdict = verify_claim_segment(segment, evidence)
        if not verdict.supported:
            continue
        key = (text, evidence_ids)
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "text": text,
                "evidence_ids": list(evidence_ids),
                "supporting_spans": [
                    {"source_id": span.source_id, "quote": span.quote}
                    for span in verdict.spans
                ],
            }
        )
        if len(claims) >= 24:
            break
    return claims


def _answerability(retrieval_status: str, evidence: Sequence[RankedEvidence]) -> str:
    if not evidence:
        return "unsupported"
    if retrieval_status == "matched":
        return "supported"
    return "partial"


def _only_ready_hits(hits: Sequence[SearchHit]) -> list[SearchHit]:
    """MySQL is authoritative if Qdrant contains stale or partially-written vectors."""
    document_ids = {hit.document_id for hit in hits}
    if not document_ids:
        return []
    with SessionLocal() as db:
        ready_ids = set(
            db.scalars(
                select(KnowledgeDocument.id)
                .join(KnowledgeBase)
                .where(
                    KnowledgeDocument.id.in_(document_ids),
                    KnowledgeDocument.status == DocumentStatus.READY,
                    KnowledgeBase.is_active.is_(True),
                )
            ).all()
        )
    return [hit for hit in hits if hit.document_id in ready_ids]


def _persist_answer(
    session_id: str,
    answer: str,
    intent: str,
    retrieval_status: str,
    evidence: Sequence[RankedEvidence],
    latency_ms: int,
    experiment: ExperimentAssignment | None = None,
    fallback_used: bool = False,
    request_id: str | None = None,
    ttft_ms: int | None = None,
    output_tokens: int | None = None,
    finish_reason: str | None = None,
) -> str:
    settings = get_settings()
    experiment = experiment or stable_assignment(settings)
    def persist(*, attach_document_ids: bool) -> str:
        with SessionLocal() as db:
            message = Message(
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=answer,
                intent=intent,
                retrieval_status=retrieval_status,
                model_name=(
                    experiment.model_version
                    if settings.llm_provider != "mock"
                    else "offline-extractive-demo"
                ),
                experiment_group=experiment.group,
                embedding_pool=experiment.embedding_pool,
                embedding_version=experiment.embedding_version,
                embedding_model=experiment.embedding_model,
                embedding_dimensions=experiment.embedding_dimensions,
                vector_index_version=experiment.vector_index_version,
                prompt_version=experiment.prompt_version,
                prompt_sha256=experiment.prompt_sha256,
                rag_version=experiment.rag_version,
                service_version=experiment.service_version,
                fallback_used=fallback_used,
                request_id=request_id,
                ttft_ms=ttft_ms,
                output_tokens=output_tokens,
                tool_calls=[],
                finish_reason=finish_reason,
                latency_ms=latency_ms,
            )
            db.add(message)
            db.flush()
            for order, item in enumerate(evidence, start=1):
                current_document_id = None
                if attach_document_ids and db.get(KnowledgeDocument, item.hit.document_id):
                    current_document_id = item.hit.document_id
                db.add(
                    MessageCitation(
                        message_id=message.id,
                        document_id=current_document_id,
                        chunk_id=item.hit.chunk_id,
                        source_name=item.hit.source_name,
                        excerpt=item.hit.summary[:500],
                        score=round(item.score * 10000),
                        display_order=order,
                    )
                )
            # Force citation rows into the same transaction before memory
            # compaction.  The invariant makes a partial answer audit record
            # impossible even under document-deletion races.
            db.flush()
            citation_count = db.scalar(
                select(func.count())
                .select_from(MessageCitation)
                .where(MessageCitation.message_id == message.id)
            )
            if citation_count != len(evidence):
                raise RuntimeError("answer citation persistence invariant failed")
            compact_session_memory(db, session_id)
            db.commit()
            return message.id

    try:
        return persist(attach_document_ids=True)
    except IntegrityError:
        # A document can be deleted after retrieval but before citation commit.
        # Citations are durable audit snapshots, so retry atomically without the
        # optional FK; session/message FK violations will fail again and remain
        # visible instead of being misreported as success.
        logger.warning("citation document changed during answer persistence; retrying snapshot-only")
        return persist(attach_document_ids=False)


async def stream_rag_answer(
    session_id: str,
    question: str,
    intent: str,
    history: Sequence[tuple[str, str]],
    knowledge_base_id: str | None,
    *,
    profile_context: str = "",
    long_memory: str = "",
    task_context: str = "",
    profile_retrieval_hints: dict[str, str] | None = None,
    experiment: ExperimentAssignment | None = None,
    request_id: str | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """Yield typed domain events; the API layer serializes them as SSE."""
    settings = get_settings()
    experiment = experiment or stable_assignment(settings)
    rag_strategy = get_rag_strategy(experiment.rag_version, settings)
    effective_experiment = experiment
    retrieval_fallback_used = False
    retrieval_top_k, retrieval_final_k = rag_strategy.top_k, rag_strategy.final_k
    start = time.perf_counter()
    rerank_trace = RerankTrace()
    route = KnowledgeRouteDecision(knowledge_base_id, "explicit" if knowledge_base_id else "global", 1.0 if knowledge_base_id else 0.0)
    yield "meta", {
        "intent": intent,
        "model": experiment.model_version,
        "experiment": experiment.public_dict(),
        "rag_strategy": rag_strategy.public_dict(),
    }
    retrieval_started = time.perf_counter()
    contextual_question = contextualize_retrieval_query(question, history)
    contextual_question = apply_profile_retrieval_hints(
        contextual_question,
        profile_retrieval_hints or {},
    )
    retrieval_plan = build_retrieval_plan(contextual_question, rag_strategy)
    retrieval_question = retrieval_plan.display_query
    input_guardrail = input_guardrail_reason(question) or input_guardrail_reason(retrieval_question)
    evidence: list[RankedEvidence]
    if input_guardrail:
        evidence = []
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)
        logger.warning("input guardrail blocked retrieval session_id=%s reason=%s", session_id, input_guardrail)
    else:
        try:
            route = await asyncio.to_thread(route_knowledge_base, retrieval_question, knowledge_base_id)
            try:
                hits = await get_vector_store(experiment.embedding_pool).query_many(
                    retrieval_plan.semantic_queries,
                    retrieval_top_k,
                    route.knowledge_base_id,
                    lexical_query=retrieval_plan.lexical_query,
                    strategy=rag_strategy,
                )
            except Exception as embedding_error:
                if experiment.embedding_pool != "canary":
                    raise
                logger.exception("canary embedding retrieval failed; retrying stable index")
                try:
                    await asyncio.to_thread(
                        record_canary_observation,
                        experiment,
                        success=False,
                        latency_ms=round((time.perf_counter() - retrieval_started) * 1000),
                        ttft_ms=None,
                        error_code=f"embedding_{type(embedding_error).__name__}",
                        timed_out=isinstance(embedding_error, (APITimeoutError, TimeoutError)),
                    )
                except Exception:
                    logger.exception("failed to record canary embedding fallback")
                stable = stable_assignment(settings)
                hits = await get_vector_store("stable").query_many(
                    retrieval_plan.semantic_queries,
                    retrieval_top_k,
                    route.knowledge_base_id,
                    lexical_query=retrieval_plan.lexical_query,
                    strategy=rag_strategy,
                )
                effective_experiment = replace(
                    experiment,
                    embedding_pool=stable.embedding_pool,
                    embedding_version=stable.embedding_version,
                    embedding_model=stable.embedding_model,
                    embedding_dimensions=stable.embedding_dimensions,
                    vector_index_version=stable.vector_index_version,
                    reason="canary_embedding_fallback",
                )
                retrieval_fallback_used = True
            raw_hit_count = len(hits)
            hits = await asyncio.to_thread(_only_ready_hits, hits)
            ready_hit_count = len(hits)
            candidate_limit = (
                rag_strategy.candidate_k
                if settings.reranker_enabled and rag_strategy.rerank_policy != "disabled"
                else retrieval_final_k
            )
            candidates = rank_evidence(retrieval_question, hits, limit=candidate_limit)
            evidence, rerank_trace = await maybe_rerank(
                retrieval_question,
                candidates,
                final_k=retrieval_final_k,
                strategy=rag_strategy,
            )
            if settings.llm_provider == "mock" and evidence:
                evidence = _offline_evidence_subset(retrieval_question, evidence)
            logger.info(
                "retrieval completed session_id=%s route=%s raw_hits=%s ready_hits=%s evidence=%s "
                "top_distance=%s strategy=%s strategy_sha=%s variants=%s rerank=%s",
                session_id,
                route.mode,
                raw_hit_count,
                ready_hit_count,
                len(evidence),
                round(min((hit.distance for hit in hits), default=2.0), 4),
                retrieval_plan.strategy,
                rag_strategy.sha256[:12],
                len(retrieval_plan.semantic_queries),
                rerank_trace.reason,
            )
            retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000)
        except Exception:
            logger.exception("retrieval failed")
            yield "error", {
                "code": "RETRIEVAL_UNAVAILABLE",
                "message": "知识检索暂时不可用，请稍后重试。",
            }
            return

    if not evidence:
        answer = (
            "我不能遵循绕过规则、编造或伪造引用、泄露系统提示或内部凭据的请求。"
            "你可以继续咨询产品使用、订单或售后问题。"
            if input_guardrail
            else settings.fallback_message
        )
        yield "sources", {"items": []}
        for index in range(0, len(answer), 4):
            yield "token", {"delta": answer[index : index + 4]}
            await asyncio.sleep(0)
        try:
            message_id = await asyncio.to_thread(
                _persist_answer,
                session_id,
                answer,
                intent,
                "empty",
                [],
                round((time.perf_counter() - start) * 1000),
                effective_experiment,
                retrieval_fallback_used,
                request_id,
                None,
                None,
                "guardrail" if input_guardrail else "fallback",
            )
        except Exception:
            logger.exception("failed to persist fallback answer")
            yield "error", {
                "code": "PERSISTENCE_FAILED",
                "message": "回答已生成，但会话保存失败，请稍后刷新确认。",
            }
            return
        yield "done", {
            "message_id": message_id,
            "grounded": True,
            "evidence_available": False,
            "handoff_recommended": not bool(input_guardrail),
            "guardrail_filtered_segments": 0,
            "answerability": "unsupported",
            "input_guardrail": input_guardrail,
            "experiment": experiment.public_dict(),
            "fallback": {
                "used": retrieval_fallback_used,
                "effective_versions": effective_experiment.public_dict(),
            },
            "generation": {
                "ttft_ms": None,
                "output_tokens": None,
                "tool_calls": [],
                "finish_reason": "guardrail" if input_guardrail else "fallback",
            },
            "claims": [],
            "reranking": {
                "attempted": rerank_trace.attempted,
                "applied": rerank_trace.applied,
                "reason": rerank_trace.reason,
            },
            "retrieval_plan": {
                "strategy": retrieval_plan.strategy,
                "semantic_query_count": len(retrieval_plan.semantic_queries),
                "expansion_count": len(retrieval_plan.expansions),
                "rag_version": retrieval_plan.rag_version,
                "strategy_sha256": retrieval_plan.rag_strategy_sha256,
                "config": rag_strategy.public_dict(),
            },
            "knowledge_route": {
                "mode": route.mode,
                "confidence": route.confidence,
                "knowledge_base_id": route.knowledge_base_id,
            },
            "suggestions": suggestions_for(intent, []),
            "timings": {
                "retrieval_ms": retrieval_ms,
                "generation_ms": 0,
                "total_ms": round((time.perf_counter() - start) * 1000),
            },
        }
        observe_chat(
            experiment_group=experiment.group,
            retrieval_status="empty",
            fallback_used=retrieval_fallback_used,
            rag_version=experiment.rag_version,
            embedding_version=effective_experiment.embedding_version,
            vector_index_version=effective_experiment.vector_index_version,
            service_version=experiment.service_version,
            model_version=experiment.model_version,
            retrieval_ms=retrieval_ms,
            generation_ms=0,
            total_ms=round((time.perf_counter() - start) * 1000),
            ttft_ms=None,
        )
        return

    public_sources = [
        {
            "source_id": item.source_id,
            "document_id": item.hit.document_id,
            "document_name": item.hit.source_name,
            "excerpt": item.hit.summary,
            "score": round(item.score, 4),
            "is_critical_rule": item.hit.priority > 0,
            "section_path": item.hit.section_path,
            "parent_expanded": bool(item.hit.parent_content),
        }
        for item in evidence
    ]
    yield "sources", {"items": public_sources}

    answer_parts: list[str] = []
    generator = get_answer_generator(experiment.service_pool)
    fallback_used = retrieval_fallback_used
    grounding_state = GroundingState()
    retrieval_status = "matched"
    generation_started = time.perf_counter()
    ttft_ms: int | None = None
    generation_trace = GenerationTrace()
    try:
        generation_question = retrieval_question if settings.llm_provider == "mock" else question
        raw_stream = generator.stream(
            generation_question,
            history,
            evidence,
            profile_context,
            long_memory,
            task_context,
            prompt_version=experiment.prompt_version,
            model_version=experiment.model_version,
            rag_strategy=rag_strategy,
            trace=generation_trace,
        )
        async for delta in _grounded_deltas(raw_stream, evidence, grounding_state):
            if ttft_ms is None:
                ttft_ms = round((time.perf_counter() - generation_started) * 1000)
            answer_parts.append(delta)
            yield "token", {"delta": delta}
        if experiment.group == "canary" and not retrieval_fallback_used:
            try:
                await asyncio.to_thread(
                    record_canary_observation,
                    experiment,
                    success=True,
                    latency_ms=round((time.perf_counter() - generation_started) * 1000),
                    ttft_ms=ttft_ms,
                )
            except Exception:
                logger.exception("failed to record successful canary observation")
    except asyncio.CancelledError:
        partial = "".join(answer_parts).strip()
        if partial:
            partial += "\n\n（连接已中断，以上为未完成回答。）"
            try:
                await asyncio.shield(
                    asyncio.to_thread(
                        _persist_answer,
                        session_id,
                        partial,
                        intent,
                        "interrupted",
                        evidence,
                        round((time.perf_counter() - start) * 1000),
                        experiment,
                        fallback_used,
                        request_id,
                        ttft_ms,
                        generation_trace.output_tokens,
                        "interrupted",
                    )
                )
            except Exception:
                logger.exception("failed to persist interrupted answer")
        raise
    except Exception as generation_error:
        logger.exception("LLM generation failed")
        if experiment.group == "canary":
            try:
                await asyncio.to_thread(
                    record_canary_observation,
                    experiment,
                    success=False,
                    latency_ms=round((time.perf_counter() - generation_started) * 1000),
                    ttft_ms=ttft_ms,
                    error_code=type(generation_error).__name__,
                    timed_out=isinstance(generation_error, (APITimeoutError, TimeoutError)),
                )
            except Exception:
                logger.exception("failed to record failed canary observation")
        had_partial_answer = bool(answer_parts)
        if not had_partial_answer and experiment.group == "canary":
            stable = stable_assignment(settings)
            effective_experiment = replace(
                effective_experiment,
                group="canary",
                model_pool=stable.model_pool,
                model_version=stable.model_version,
                prompt_version=stable.prompt_version,
                prompt_sha256=stable.prompt_sha256,
                service_pool=stable.service_pool,
                service_version=stable.service_version,
                buckets=experiment.buckets,
                reason="canary_generation_fallback",
            )
            fallback_used = True
            retrieval_status = "canary_fallback"
            try:
                generation_trace = GenerationTrace()
                stable_stream = get_answer_generator("stable").stream(
                    question,
                    history,
                    evidence,
                    profile_context,
                    long_memory,
                    task_context,
                    prompt_version=stable.prompt_version,
                    model_version=stable.model_version,
                    rag_strategy=rag_strategy,
                    trace=generation_trace,
                )
                async for delta in _grounded_deltas(stable_stream, evidence, grounding_state):
                    answer_parts.append(delta)
                    yield "token", {"delta": delta}
            except Exception:
                logger.exception("stable LLM fallback failed")
        if not answer_parts:
            degraded_answer = _extractive_answer(question, evidence, degraded=True)
            answer_parts.append(degraded_answer)
            retrieval_status = "generation_fallback"
            fallback_used = True
            for index in range(0, len(degraded_answer), 12):
                yield "token", {"delta": degraded_answer[index : index + 12]}
        elif had_partial_answer:
            warning = "\n\n生成服务中断，以上仅为已通过引用校验的部分回答。"
            answer_parts.append(warning)
            retrieval_status = "generation_partial"
            yield "token", {"delta": warning}

    answer = "".join(answer_parts).strip()
    generation_ms = round((time.perf_counter() - generation_started) * 1000)
    if grounding_state.removed_segments:
        warning = f"\n\n> 安全护栏已过滤 {grounding_state.removed_segments} 段缺少有效引用的生成内容。"
        # A model can place one citation after several sentences.  The strict
        # per-segment guard correctly removes the earlier uncited claims, but
        # must not leave the customer with missing procedure steps.  Append a
        # deterministic, sentence-level cited extract from the same evidence;
        # this restores utility without trusting the rejected model text.
        supplement = "\n\n" + _extractive_answer(question, evidence)
        answer += warning + supplement
        yield "token", {"delta": warning + supplement}
        if retrieval_status == "matched":
            retrieval_status = "grounded_filtered"
    if not _valid_citations(answer, evidence):
        replacement = _extractive_answer(question, evidence)
        if answer:
            replacement = "\n\n" + replacement
        answer += replacement
        yield "token", {"delta": replacement}
        retrieval_status = "grounded_filtered"
    grounded = True
    try:
        message_id = await asyncio.to_thread(
            _persist_answer,
            session_id,
            answer,
            intent,
            retrieval_status,
            evidence,
            round((time.perf_counter() - start) * 1000),
            effective_experiment,
            fallback_used,
            request_id,
            ttft_ms,
            generation_trace.output_tokens,
            generation_trace.finish_reason
            or ("fallback" if fallback_used else "stop"),
        )
    except Exception:
        logger.exception("failed to persist generated answer")
        yield "error", {
            "code": "PERSISTENCE_FAILED",
            "message": "回答已生成，但会话保存失败，请稍后刷新确认。",
        }
        return
    source_names = list(dict.fromkeys(item.hit.source_name for item in evidence))
    if _should_shadow(experiment, intent):
        _schedule_shadow(
            stable_message_id=message_id,
            question=question,
            retrieval_question=contextual_question,
            knowledge_base_id=route.knowledge_base_id,
            stable_answer=answer,
            buckets=experiment.buckets,
        )
    total_ms = round((time.perf_counter() - start) * 1000)
    observe_chat(
        experiment_group=experiment.group,
        retrieval_status=retrieval_status,
        fallback_used=fallback_used,
        rag_version=effective_experiment.rag_version,
        embedding_version=effective_experiment.embedding_version,
        vector_index_version=effective_experiment.vector_index_version,
        service_version=effective_experiment.service_version,
        model_version=effective_experiment.model_version,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=total_ms,
        ttft_ms=ttft_ms,
    )
    yield "done", {
        "message_id": message_id,
        "grounded": grounded,
        "evidence_available": True,
        "handoff_recommended": intent == "投诉" or retrieval_status != "matched",
        "guardrail_filtered_segments": grounding_state.removed_segments,
        "answerability": _answerability(retrieval_status, evidence),
        "input_guardrail": input_guardrail,
        "experiment": experiment.public_dict(),
        "fallback": {
            "used": fallback_used,
            "effective_versions": effective_experiment.public_dict(),
        },
        "generation": {
            "ttft_ms": ttft_ms,
            "output_tokens": generation_trace.output_tokens,
            "tool_calls": [],
            "finish_reason": generation_trace.finish_reason
            or ("fallback" if fallback_used else "stop"),
        },
        "claims": _structured_claims(answer, evidence),
        "reranking": {
            "attempted": rerank_trace.attempted,
            "applied": rerank_trace.applied,
            "reason": rerank_trace.reason,
        },
        "retrieval_plan": {
            "strategy": retrieval_plan.strategy,
            "semantic_query_count": len(retrieval_plan.semantic_queries),
            "expansion_count": len(retrieval_plan.expansions),
            "rag_version": retrieval_plan.rag_version,
            "strategy_sha256": retrieval_plan.rag_strategy_sha256,
            "config": rag_strategy.public_dict(),
        },
        "knowledge_route": {
            "mode": route.mode,
            "confidence": route.confidence,
            "knowledge_base_id": route.knowledge_base_id,
        },
        "suggestions": suggestions_for(intent, source_names),
        "timings": {
            "retrieval_ms": retrieval_ms,
            "generation_ms": generation_ms,
            "total_ms": total_ms,
        },
    }
