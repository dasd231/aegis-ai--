from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Feedback, Message, MessageRole


_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[邮箱已脱敏]"),
    (re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), "[手机号已脱敏]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "[证件号已脱敏]"),
    (re.compile(r"(?i)\b(?:bearer\s+|sk-)[A-Za-z0-9._-]{12,}\b"), "[凭据已脱敏]"),
    (re.compile(r"(?<!\d)\d{12,}(?!\d)"), "[长编号已脱敏]"),
)


def redact_training_text(value: str | None) -> str:
    text = value or ""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _question_before(db: Session, answer: Message) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.session_id == answer.session_id,
            Message.role == MessageRole.USER,
            Message.created_at <= answer.created_at,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    )


def build_feedback_dataset(
    db: Session,
    *,
    rating: int = -1,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Build review-only, de-identified examples from explicit user feedback.

    No sample is marked optimization-ready automatically. A reviewer must label
    the failure category, expected answer, and evidence sufficiency first.
    """
    feedback_rows = db.scalars(
        select(Feedback)
        .options(selectinload(Feedback.message).selectinload(Message.citations))
        .where(Feedback.rating == rating)
        .order_by(Feedback.updated_at.asc())
        .limit(limit)
    ).all()
    samples: list[dict[str, Any]] = []
    for feedback in feedback_rows:
        answer = feedback.message
        question = _question_before(db, answer)
        if question is None:
            continue
        sample_id = hashlib.sha256(feedback.id.encode("utf-8")).hexdigest()[:20]
        samples.append(
            {
                "schema_version": "feedback-example-v1",
                "sample_id": sample_id,
                "question": redact_training_text(question.content),
                "answer": redact_training_text(answer.content),
                "rating": feedback.rating,
                "user_comment": redact_training_text(feedback.comment),
                "intent": answer.intent,
                "retrieval_status": answer.retrieval_status,
                "versions": {
                    "model": answer.model_name,
                    "embedding_pool": answer.embedding_pool,
                    "embedding": answer.embedding_version,
                    "embedding_model": answer.embedding_model,
                    "vector_index": answer.vector_index_version,
                    "prompt": answer.prompt_version,
                    "prompt_sha256": answer.prompt_sha256,
                    "rag": answer.rag_version,
                    "service": answer.service_version,
                    "experiment_group": answer.experiment_group,
                },
                "evidence": [
                    {
                        "source_name": citation.source_name,
                        "excerpt": redact_training_text(citation.excerpt),
                        "score": citation.score,
                    }
                    for citation in sorted(answer.citations, key=lambda item: item.display_order)
                ],
                "review": {
                    "status": "pending",
                    "failure_category": None,
                    "evidence_sufficient": None,
                    "expected_answer": None,
                    "eligible_for_optimization": False,
                },
            }
        )
    return samples
