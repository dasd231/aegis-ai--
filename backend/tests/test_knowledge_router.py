from __future__ import annotations

from app.services.knowledge_router import KnowledgeBaseDescriptor, choose_knowledge_base


BASES = [
    KnowledgeBaseDescriptor("refund", "售后退款知识库", "退款、退换货与服务补偿规则"),
    KnowledgeBaseDescriptor("invoice", "财税发票知识库", "电子发票、增值税专票与开票要求"),
]


def test_router_uses_exact_metadata_name_without_an_llm():
    decision = choose_knowledge_base("请查售后退款知识库中的七天退款条件", BASES)
    assert decision.knowledge_base_id == "refund"
    assert decision.mode == "metadata_exact"
    assert decision.confidence == 1.0


def test_router_falls_back_to_global_recall_when_metadata_is_ambiguous():
    decision = choose_knowledge_base("企业服务有什么要求？", BASES)
    assert decision.knowledge_base_id is None
    assert decision.mode == "global"
