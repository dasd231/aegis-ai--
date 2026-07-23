from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.config import get_settings
from app.services.rag import maybe_rerank, rank_evidence
from app.services.reranker import EvidenceReranker
from app.services.resilience import AsyncBulkhead, CircuitBreaker
from tests.test_rag import hit


class FakeCompletions:
    def __init__(self, content: str):
        self.content = content

    async def create(self, **_):
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


class FakeRerankResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "results": [
                {"index": 1, "relevance_score": 0.15},
                {"index": 0, "relevance_score": 0.92},
            ]
        }


class FakeRerankHttpClient:
    def __init__(self):
        self.request = None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeRerankResponse()


def test_reranker_validates_complete_bounded_scores():
    reranker = EvidenceReranker()
    reranker.client = FakeClient(
        '{"scores":[{"id":"a","relevance":1.2},{"id":"b","relevance":0.25}]}'
    )  # type: ignore[assignment]
    scores = asyncio.run(
        reranker.score(
            "退款条件",
            [{"id": "a", "section": "退款", "text": "七日"}, {"id": "b", "section": "其他", "text": "发票"}],
        )
    )
    assert scores == {"a": 1.0, "b": 0.25}


def test_reranker_rejects_partial_model_output_and_fails_open():
    reranker = EvidenceReranker()
    reranker.client = FakeClient('{"scores":[{"id":"a","relevance":0.8}]}')  # type: ignore[assignment]
    scores = asyncio.run(
        reranker.score(
            "退款条件",
            [{"id": "a", "section": "退款", "text": "七日"}, {"id": "b", "section": "其他", "text": "发票"}],
        )
    )
    assert scores is None


def test_reranker_uses_explicit_dependency_target(monkeypatch):
    calls: list[tuple[str, str, str | None]] = []
    breaker = CircuitBreaker()
    bulkhead = AsyncBulkhead(limit=1, wait_seconds=0.1)

    def capture_breaker(pool: str = "stable", target: str | None = None):
        calls.append(("breaker", pool, target))
        return breaker

    def capture_bulkhead(pool: str = "stable", target: str | None = None):
        calls.append(("bulkhead", pool, target))
        return bulkhead

    monkeypatch.setattr("app.services.reranker.reranker_breaker", capture_breaker)
    monkeypatch.setattr("app.services.reranker.reranker_bulkhead", capture_bulkhead)
    reranker = EvidenceReranker()
    reranker.client = FakeClient(
        '{"scores":[{"id":"a","relevance":0.9},{"id":"b","relevance":0.2}]}'
    )  # type: ignore[assignment]

    scores = asyncio.run(
        reranker.score(
            "退款条件",
            [{"id": "a", "section": "退款", "text": "七日"}, {"id": "b", "section": "其他", "text": "发票"}],
        )
    )

    assert scores == {"a": 0.9, "b": 0.2}
    assert [kind for kind, _, _ in calls] == ["breaker", "bulkhead"]
    assert all(pool == "stable" for _, pool, _ in calls)
    assert calls[0][2] == calls[1][2]
    assert calls[0][2] not in {None, "", "default"}


def test_dashscope_native_reranker_maps_indices_and_uses_workspace_origin(monkeypatch):
    reranker = EvidenceReranker()
    fake = FakeRerankHttpClient()
    monkeypatch.setattr(
        reranker,
        "_dashscope_runtime",
        lambda: (
            fake,
            "secret-not-logged",
            "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
    )
    scores = asyncio.run(
        reranker._score_dashscope(
            "退款条件",
            [
                {"id": "refund", "section": "退款", "text": "七日"},
                {"id": "battery", "section": "电池", "text": "停止充电"},
            ],
        )
    )
    assert scores == {"refund": 0.92, "battery": 0.15}
    assert fake.request is not None
    assert fake.request[0] == "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks"
    assert fake.request[1]["json"]["top_n"] == 2


def test_conditional_reranker_integration_applies_scores(monkeypatch):
    settings = get_settings()
    original_enabled = settings.reranker_enabled
    original_provider = settings.llm_provider

    class StubReranker:
        async def score(self, _question, documents):
            return {item["id"]: (1.0 if item["id"] == "b" else 0.1) for item in documents}

    monkeypatch.setattr("app.services.rag.get_evidence_reranker", lambda: StubReranker())
    settings.reranker_enabled = True
    settings.llm_provider = "openai_compatible"
    try:
        candidates = rank_evidence(
            "标准版和企业版分别有什么区别？",
            [
                hit("a", "标准版支持基础功能。", 0.20),
                hit("b", "企业版支持专属功能。", 0.21),
                hit("c", "标准版提供试用。", 0.22),
                hit("d", "企业版支持定制。", 0.23),
            ],
            limit=4,
        )
        reranked, trace = asyncio.run(maybe_rerank("标准版和企业版分别有什么区别？", candidates))
        assert trace.applied is True
        assert reranked[0].hit.chunk_id == "b"
    finally:
        settings.reranker_enabled = original_enabled
        settings.llm_provider = original_provider
