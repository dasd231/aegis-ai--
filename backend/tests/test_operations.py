from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.services.resilience import embedding_breaker, embedding_bulkhead
from scripts.deployment_smoke import parse_sse


def test_health_reports_dependencies_capacity_and_ingestion_queue(client: TestClient):
    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")
    capacity = client.get("/api/v1/health/capacity")

    assert live.status_code == 200 and live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json()["components"] == {
        "database": "ready",
        "vector_store": {"stable": "ready"},
    }
    assert capacity.status_code == 200
    assert capacity.json()["requests"]["limit"] > 0
    assert capacity.json()["dependencies"]["llm_limit"] > 0
    assert capacity.json()["dependencies"]["embedding_limit"] > 0
    assert capacity.json()["dependencies"]["embedding_circuit"] == "closed"
    dependency_pools = capacity.json()["dependencies"]["pools"]
    assert set(dependency_pools["llm"]) == {"stable", "canary"}
    assert set(dependency_pools["embedding"]) == {"stable", "canary"}
    assert dependency_pools["embedding"]["stable"]["limit"] > 0
    assert dependency_pools["embedding"]["canary"]["limit"] > 0
    retrieval_cache = capacity.json()["retrieval_cache"]["stable"]
    assert retrieval_cache["limit"] > 0
    assert retrieval_cache["ttl_seconds"] > 0
    assert 0 <= retrieval_cache["hit_rate"] <= 1
    queue = capacity.json()["ingestion_queue"]
    assert queue["pending"] == 0
    assert queue["expired_leases"] == 0
    assert queue["oldest_pending_age_seconds"] is None


def test_prometheus_endpoint_exposes_chat_and_canary_metrics(client: TestClient):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "aegis_chat_requests_total" in response.text
    assert "aegis_chat_ttft_seconds" in response.text
    assert "aegis_canary_enabled 1.0" in response.text


def test_deployment_smoke_parser_handles_crlf_and_multiline_data():
    events = parse_sse(
        'event: token\r\ndata: {"delta":"退款"}\r\n\r\n'
        'event: done\r\ndata: {"grounded":true,\r\ndata: "answerability":"supported"}\r\n\r\n'
    )
    assert [(event.name, event.data) for event in events] == [
        ("token", {"delta": "退款"}),
        ("done", {"grounded": True, "answerability": "supported"}),
    ]


def test_canary_failures_do_not_open_stable_breaker():
    target = f"test-isolation-{uuid4().hex}"
    stable = embedding_breaker("stable", target=target)
    canary = embedding_breaker("canary", target=target)

    for _ in range(canary.failure_threshold):
        call_generation = canary.before_call()
        canary.failed(call_generation)

    assert canary.state == "open"
    assert stable.state == "closed"
    stable_call = stable.before_call()
    stable.succeeded(stable_call)


def test_saturated_canary_bulkhead_does_not_block_stable(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_max_concurrency", 1)
    monkeypatch.setattr(settings, "embedding_queue_timeout_seconds", 0.1)
    target = f"test-isolation-{uuid4().hex}"

    async def exercise() -> None:
        canary = embedding_bulkhead("canary", target=target)
        stable = embedding_bulkhead("stable", target=target)
        assert canary is not stable
        async with canary.slot():
            assert canary.active == canary.limit == 1
            async with stable.slot():
                assert stable.active == 1
                assert canary.active == 1

    asyncio.run(exercise())
