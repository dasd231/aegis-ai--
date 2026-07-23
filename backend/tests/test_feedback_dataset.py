from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    ConversationSession,
    Feedback,
    Message,
    MessageCitation,
    MessageRole,
    User,
)
from app.services.feedback_dataset import build_feedback_dataset, redact_training_text


def test_redaction_removes_common_credentials_and_identifiers():
    text = "邮箱 a@example.com 手机 13800138000，token sk-abcdefghijklmnop，订单 12345678901234"
    redacted = redact_training_text(text)
    assert "a@example.com" not in redacted
    assert "13800138000" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    assert "12345678901234" not in redacted


def test_negative_feedback_export_keeps_versions_and_requires_review():
    with SessionLocal() as db:
        user = User(email="dataset@example.com", password_hash="unused", display_name="Dataset")
        db.add(user)
        db.flush()
        conversation = ConversationSession(user_id=user.id, title="dataset")
        db.add(conversation)
        db.flush()
        question = Message(
            session_id=conversation.id,
            role=MessageRole.USER,
            content="我的手机号 13900139000，退款多久？",
        )
        answer = Message(
            session_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="需要七天。[S1]",
            intent="售后问题",
            retrieval_status="matched",
            model_name="qwen-stable",
            experiment_group="stable",
            embedding_pool="canary",
            embedding_version="qwen37-512-v1",
            embedding_model="qwen3.7-text-embedding",
            embedding_dimensions=512,
            vector_index_version="knowledge-qwen37-512-v1",
            prompt_version="customer_v1",
            rag_version="hybrid_v1",
            service_version="vllm-stable",
        )
        db.add_all([question, answer])
        db.flush()
        db.add(
            MessageCitation(
                message_id=answer.id,
                document_id=None,
                chunk_id="c1",
                source_name="售后规则.md",
                excerpt="退款处理规则",
                score=9000,
                display_order=1,
            )
        )
        db.add(
            Feedback(
                message_id=answer.id,
                user_id=user.id,
                rating=-1,
                comment="联系我 13900139000，答案不完整",
            )
        )
        db.commit()

        samples = build_feedback_dataset(db)

    assert len(samples) == 1
    sample = samples[0]
    assert sample["versions"]["prompt"] == "customer_v1"
    assert sample["versions"]["embedding"] == "qwen37-512-v1"
    assert sample["versions"]["vector_index"] == "knowledge-qwen37-512-v1"
    assert sample["evidence"][0]["source_name"] == "售后规则.md"
    assert "13900139000" not in sample["question"]
    assert "13900139000" not in sample["user_comment"]
    assert sample["review"]["status"] == "pending"
    assert sample["review"]["eligible_for_optimization"] is False
    assert "user_id" not in sample
