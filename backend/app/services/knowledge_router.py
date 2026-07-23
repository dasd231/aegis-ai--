from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DocumentStatus, KnowledgeBase, KnowledgeDocument


_GENERIC_TERMS = {
    "知识",
    "资料",
    "相关",
    "企业",
    "服务",
    "系统",
    "说明",
    "客服",
    "数据",
    "文档",
}


@dataclass(frozen=True)
class KnowledgeBaseDescriptor:
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class KnowledgeRouteDecision:
    knowledge_base_id: str | None
    mode: str
    confidence: float


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _route_terms(text: str) -> set[str]:
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms - _GENERIC_TERMS


def choose_knowledge_base(
    question: str,
    candidates: list[KnowledgeBaseDescriptor],
) -> KnowledgeRouteDecision:
    """Use only high-confidence metadata routes; ambiguity safely falls back to global recall."""
    if not candidates:
        return KnowledgeRouteDecision(None, "global", 0.0)
    normalized_question = _normalized(question)
    for candidate in sorted(candidates, key=lambda item: len(_normalized(item.name)), reverse=True):
        normalized_name = _normalized(candidate.name)
        short_name = re.sub(r"(?:企业)?(?:知识库|资料库)$", "", normalized_name)
        if len(short_name) >= 3 and short_name in normalized_question:
            return KnowledgeRouteDecision(candidate.id, "metadata_exact", 1.0)

    question_terms = _route_terms(question)
    if len(question_terms) < 2:
        return KnowledgeRouteDecision(None, "global", 0.0)
    ranked: list[tuple[float, int, str]] = []
    for candidate in candidates:
        metadata_terms = _route_terms(f"{candidate.name} {candidate.description}")
        overlap = question_terms & metadata_terms
        score = sum(len(term) for term in overlap) / max(1, sum(len(term) for term in question_terms))
        ranked.append((score, len(overlap), candidate.id))
    ranked.sort(reverse=True)
    best_score, match_count, best_id = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if match_count >= 2 and best_score >= 0.5 and best_score - second_score >= 0.15:
        return KnowledgeRouteDecision(best_id, "metadata_terms", round(best_score, 4))
    return KnowledgeRouteDecision(None, "global", round(best_score, 4))


def route_knowledge_base(question: str, requested_id: str | None) -> KnowledgeRouteDecision:
    if requested_id:
        return KnowledgeRouteDecision(requested_id, "explicit", 1.0)
    with SessionLocal() as db:
        rows = db.execute(
            select(KnowledgeBase.id, KnowledgeBase.name, KnowledgeBase.description)
            .join(KnowledgeDocument)
            .where(
                KnowledgeBase.is_active.is_(True),
                KnowledgeDocument.status == DocumentStatus.READY,
            )
            .distinct()
        ).all()
    candidates = [KnowledgeBaseDescriptor(id=row.id, name=row.name, description=row.description) for row in rows]
    return choose_knowledge_base(question, candidates)
