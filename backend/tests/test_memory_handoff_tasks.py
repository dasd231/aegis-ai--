from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest

from app.database import SessionLocal
from app.models import (
    DocumentStatus,
    IngestionJobStatus,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeIngestionJob,
    Message,
    MessageRole,
    SessionMemory,
    utcnow,
)
from app.services.job_queue import (
    claim_ingestion_job,
    enqueue_ingestion,
    finish_ingestion_job,
    ingestion_queue_snapshot,
    renew_ingestion_lease,
)
from app.services.knowledge import IngestionFence, process_document
from app.services.rag import GroundingState, _grounded_deltas
from app.services.resilience import AsyncBulkhead, BulkheadFullError, CircuitBreaker, CircuitOpenError
from app.services.tool_sandbox import ToolPolicyError, ToolSandbox, ToolSpec
from tests.conftest import auth


def test_profile_consent_controls_long_memory(client: TestClient, user_token: str):
    me = client.get("/api/v1/auth/me", headers=auth(user_token)).json()
    profile = client.get("/api/v1/profile", headers=auth(user_token))
    assert profile.status_code == 200
    assert profile.json()["memory_consent"] is False
    enabled = client.put(
        "/api/v1/profile",
        json={
            "preferred_name": "测试用户",
            "locale": "zh-CN",
            "timezone": "Asia/Hong_Kong",
            "response_style": "concise",
            "memory_consent": True,
            "consent_version": "privacy-v1",
        },
        headers=auth(user_token),
    )
    assert enabled.status_code == 200
    session_id = client.post("/api/v1/sessions", json={"title": "memory"}, headers=auth(user_token)).json()["id"]
    with SessionLocal() as db:
        for index in range(14):
            db.add(
                Message(
                    session_id=session_id,
                    role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                    content=f"历史内容 {index}",
                )
            )
        db.commit()
    streamed = client.post(
        "/api/v1/chat/stream",
        json={"session_id": session_id, "question": "没有资料的问题"},
        headers=auth(user_token),
    )
    assert streamed.status_code == 200
    with SessionLocal() as db:
        memory = db.get(SessionMemory, session_id)
        assert memory is not None
        assert memory.compacted_message_count >= 2
        assert "不是知识库事实" in memory.summary
    forgotten = client.delete("/api/v1/profile/memory", headers=auth(user_token))
    assert forgotten.status_code == 204
    with SessionLocal() as db:
        assert db.get(SessionMemory, session_id) is None
    assert client.get("/api/v1/profile", headers=auth(user_token)).json()["memory_consent"] is False
    assert me["id"]


def test_task_memory_uses_optimistic_versioning(client: TestClient, user_token: str):
    session_id = client.post("/api/v1/sessions", json={"title": "task"}, headers=auth(user_token)).json()["id"]
    created = client.post(
        f"/api/v1/sessions/{session_id}/tasks",
        json={"task_type": "refund_request", "slots": {"order_id": "A-1"}},
        headers=auth(user_token),
    )
    assert created.status_code == 201
    task = created.json()
    updated = client.patch(
        f"/api/v1/sessions/{session_id}/tasks/{task['id']}",
        json={"expected_version": 1, "slots": {"order_id": "A-1", "reason": "duplicate"}},
        headers=auth(user_token),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    stale = client.patch(
        f"/api/v1/sessions/{session_id}/tasks/{task['id']}",
        json={"expected_version": 1, "status": "completed"},
        headers=auth(user_token),
    )
    assert stale.status_code == 409


def test_handoff_is_consent_driven_idempotent_and_admin_managed(
    client: TestClient, user_token: str, admin_token: str
):
    session_id = client.post("/api/v1/sessions", json={"title": "handoff"}, headers=auth(user_token)).json()["id"]
    payload = {"session_id": session_id, "trigger": "manual", "reason": "需要人工确认退款", "priority": 4}
    first = client.post("/api/v1/handoffs", json=payload, headers=auth(user_token))
    second = client.post("/api/v1/handoffs", json=payload, headers=auth(user_token))
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    admin = client.get("/api/v1/auth/me", headers=auth(admin_token)).json()
    queue = client.get("/api/v1/admin/handoffs?status=open", headers=auth(admin_token))
    assert queue.status_code == 200
    assert queue.json()[0]["id"] == first.json()["id"]
    claimed = client.patch(
        f"/api/v1/admin/handoffs/{first.json()['id']}",
        json={"expected_version": first.json()["version"], "status": "in_progress", "assigned_to": admin["id"]},
        headers=auth(admin_token),
    )
    assert claimed.status_code == 200
    assert claimed.json()["assigned_to"] == admin["id"]
    assert claimed.json()["version"] == 2
    stale = client.patch(
        f"/api/v1/admin/handoffs/{first.json()['id']}",
        json={"expected_version": 1, "status": "resolved", "resolution": "过期管理员结论"},
        headers=auth(admin_token),
    )
    assert stale.status_code == 409
    resolved = client.patch(
        f"/api/v1/admin/handoffs/{first.json()['id']}",
        json={"expected_version": claimed.json()["version"], "status": "resolved", "resolution": "已人工核实"},
        headers=auth(admin_token),
    )
    assert resolved.status_code == 200
    assert resolved.json()["version"] == 3
    reopened = client.post("/api/v1/handoffs", json=payload, headers=auth(user_token))
    assert reopened.status_code == 201
    assert reopened.json()["id"] != first.json()["id"]


def test_handoff_rejects_illegal_state_and_incomplete_resolution(
    client: TestClient, user_token: str, admin_token: str
):
    session_id = client.post("/api/v1/sessions", json={"title": "state-machine"}, headers=auth(user_token)).json()[
        "id"
    ]
    created = client.post(
        "/api/v1/handoffs",
        json={"session_id": session_id, "trigger": "manual", "reason": "需要专员处理"},
        headers=auth(user_token),
    ).json()
    admin = client.get("/api/v1/auth/me", headers=auth(admin_token)).json()

    skipped_claim = client.patch(
        f"/api/v1/admin/handoffs/{created['id']}",
        json={"expected_version": created["version"], "status": "resolved", "resolution": "试图跳级"},
        headers=auth(admin_token),
    )
    assert skipped_claim.status_code == 409
    missing_assignee = client.patch(
        f"/api/v1/admin/handoffs/{created['id']}",
        json={"expected_version": created["version"], "status": "in_progress"},
        headers=auth(admin_token),
    )
    assert missing_assignee.status_code == 422
    claimed = client.patch(
        f"/api/v1/admin/handoffs/{created['id']}",
        json={"expected_version": created["version"], "status": "in_progress", "assigned_to": admin["id"]},
        headers=auth(admin_token),
    ).json()
    missing_resolution = client.patch(
        f"/api/v1/admin/handoffs/{created['id']}",
        json={"expected_version": claimed["version"], "status": "resolved"},
        headers=auth(admin_token),
    )
    assert missing_resolution.status_code == 422
    invalid_assignee = client.patch(
        f"/api/v1/admin/handoffs/{created['id']}",
        json={"expected_version": claimed["version"], "assigned_to": created["user_id"]},
        headers=auth(admin_token),
    )
    assert invalid_assignee.status_code == 422


def test_admin_can_audit_all_sessions_but_user_cannot(client: TestClient, user_token: str, admin_token: str):
    session_id = client.post("/api/v1/sessions", json={"title": "audited"}, headers=auth(user_token)).json()["id"]
    denied = client.get("/api/v1/admin/sessions", headers=auth(user_token))
    assert denied.status_code == 403
    page = client.get("/api/v1/admin/sessions?page=1&page_size=20", headers=auth(admin_token))
    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["id"] == session_id
    detail = client.get(f"/api/v1/admin/sessions/{session_id}", headers=auth(admin_token))
    assert detail.status_code == 200
    assert detail.json()["user_display_name"] == "Tester"
    audits = client.get("/api/v1/admin/data-access-audits", headers=auth(admin_token))
    assert audits.status_code == 200
    assert {row["action"] for row in audits.json()} == {"session_list", "session_read"}
    assert all(row["request_id"] for row in audits.json())


def test_grounding_filter_removes_uncited_claims():
    from tests.test_rag import hit
    from app.services.rag import rank_evidence

    evidence = rank_evidence("退款期限", [hit("rule", "退款必须在七日内申请。", 0.2, priority=1)])

    async def raw():
        yield "退款可在三十日内申请。\n"
        yield "知识库明确要求七日内申请 [S1]。\n"

    async def collect():
        state = GroundingState()
        output = "".join([item async for item in _grounded_deltas(raw(), evidence, state)])
        return output, state

    output, state = asyncio.run(collect())
    assert "三十日" not in output
    assert "七日" in output
    assert state.removed_segments == 1
    assert state.cited_segments == 1


def test_grounding_filter_waits_for_citation_in_a_later_stream_chunk():
    from tests.test_rag import hit
    from app.services.rag import rank_evidence

    evidence = rank_evidence("退款期限", [hit("rule", "退款必须在七日内申请。", 0.2, priority=1)])

    async def raw():
        yield "退款必须在七日内申请。"
        yield " "
        yield "[S"
        yield "1]\n"

    async def collect():
        state = GroundingState()
        output = "".join([item async for item in _grounded_deltas(raw(), evidence, state)])
        return output, state

    output, state = asyncio.run(collect())
    assert output == "退款必须在七日内申请。 [S1]\n"
    assert state.removed_segments == 0
    assert state.cited_segments == 1


def test_tool_sandbox_enforces_allowlist_role_and_output_contract():
    sandbox = ToolSandbox(max_concurrency=1)

    async def echo(payload):
        return {"value": payload["value"]}

    def validate(payload):
        if not isinstance(payload.get("value"), str):
            raise ToolPolicyError("value must be string")

    sandbox.register(ToolSpec(name="echo", handler=echo, validator=validate, allowed_roles=frozenset({"admin"})))
    assert asyncio.run(sandbox.execute("echo", {"value": "ok"}, role="admin", user_id="u")) == {"value": "ok"}
    try:
        asyncio.run(sandbox.execute("echo", {"value": "blocked"}, role="user", user_id="u"))
    except ToolPolicyError:
        pass
    else:
        raise AssertionError("role policy was not enforced")


def test_circuit_breaker_and_bulkhead_fail_fast():
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    breaker.failed(breaker.before_call())
    breaker.failed(breaker.before_call())
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    breaker.before_call()

    async def contend():
        bulkhead = AsyncBulkhead(limit=1, wait_seconds=0.01)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with bulkhead.slot():
                entered.set()
                await release.wait()

        task = asyncio.create_task(holder())
        await entered.wait()
        try:
            with pytest.raises(BulkheadFullError):
                async with bulkhead.slot():
                    raise AssertionError("unreachable")
        finally:
            release.set()
            await task

    asyncio.run(contend())


def test_circuit_breaker_half_open_allows_one_probe_and_reopens(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("app.services.resilience.time.monotonic", lambda: clock["now"])
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=10)

    first_generation = breaker.before_call()
    breaker.failed(first_generation)
    breaker.failed(breaker.before_call())
    assert breaker.state == "open"
    # A slow request from the prior generation cannot close a newer circuit.
    breaker.succeeded(first_generation)
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()

    clock["now"] = 111.0
    assert breaker.state == "half_open"
    probe = breaker.before_call()
    with pytest.raises(CircuitOpenError, match="half-open"):
        breaker.before_call()
    breaker.failed(probe)
    assert breaker.state == "open"

    clock["now"] = 122.0
    probe = breaker.before_call()
    breaker.abandoned(probe)
    assert breaker.state == "open"
    clock["now"] = 133.0
    probe = breaker.before_call()
    breaker.succeeded(probe)
    assert breaker.state == "closed"


def test_ingestion_queue_claim_process_and_finish(tmp_path, client: TestClient, admin_token: str):
    admin_id = client.get("/api/v1/auth/me", headers=auth(admin_token)).json()["id"]
    source = tmp_path / "queued.md"
    source.write_text("# 退款规则\n用户应当在签收后七日内申请退款。", encoding="utf-8")
    with SessionLocal() as db:
        base = KnowledgeBase(name="queue-base", description="queue test")
        db.add(base)
        db.flush()
        document = KnowledgeDocument(
            knowledge_base_id=base.id,
            original_name=source.name,
            storage_path=str(source),
            content_type="text/markdown",
            size_bytes=source.stat().st_size,
            sha256="a" * 64,
            status=DocumentStatus.PROCESSING,
            uploaded_by=admin_id,
        )
        db.add(document)
        db.flush()
        enqueue_ingestion(db, document.id)
        document_id = document.id
        db.commit()

    claim = claim_ingestion_job("test-worker")
    assert claim is not None
    assert claim.document_id == document_id
    assert claim.lease_owner == "test-worker"
    assert renew_ingestion_lease(claim) is True
    assert renew_ingestion_lease(replace(claim, lease_owner="expired-worker")) is False
    finish_ingestion_job(replace(claim, lease_owner="expired-worker"), False)
    with SessionLocal() as db:
        assert db.get(KnowledgeIngestionJob, claim.id).status == IngestionJobStatus.RUNNING
    stale = IngestionFence(claim.id, "expired-worker", claim.attempts)
    assert asyncio.run(process_document(document_id, stale)) is False
    succeeded = asyncio.run(
        process_document(document_id, IngestionFence(claim.id, claim.lease_owner, claim.attempts))
    )
    finish_ingestion_job(claim, succeeded)

    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        job = db.get(KnowledgeIngestionJob, claim.id)
        assert document is not None and document.status == DocumentStatus.READY
        assert document.chunk_count > 0
        assert job is not None and job.status == IngestionJobStatus.COMPLETED
        assert job.lease_owner is None
    snapshot = ingestion_queue_snapshot()
    assert snapshot["completed"] == 1
    assert snapshot["pending"] == 0


def test_stale_ingestion_cannot_commit_after_vector_write(
    tmp_path, client: TestClient, admin_token: str, monkeypatch
):
    from app.services.vector_store import get_vector_store

    admin_id = client.get("/api/v1/auth/me", headers=auth(admin_token)).json()["id"]
    source = tmp_path / "fenced.md"
    source.write_text("退款必须由企业管理员提交申请。", encoding="utf-8")
    with SessionLocal() as db:
        base = KnowledgeBase(name="fenced-base", description="fencing test")
        db.add(base)
        db.flush()
        document = KnowledgeDocument(
            knowledge_base_id=base.id,
            original_name=source.name,
            storage_path=str(source),
            content_type="text/markdown",
            size_bytes=source.stat().st_size,
            sha256="c" * 64,
            status=DocumentStatus.PROCESSING,
            uploaded_by=admin_id,
        )
        db.add(document)
        db.flush()
        enqueue_ingestion(db, document.id)
        document_id = document.id
        db.commit()

    claim = claim_ingestion_job("old-worker")
    assert claim is not None
    store = get_vector_store()
    original_upsert = store.upsert_document

    async def reclaim_after_write(*args, **kwargs):
        await original_upsert(*args, **kwargs)
        with SessionLocal() as db:
            job = db.get(KnowledgeIngestionJob, claim.id)
            assert job is not None
            job.lease_owner = "new-worker"
            job.attempts += 1
            job.lease_expires_at = utcnow() + timedelta(minutes=5)
            db.commit()

    monkeypatch.setattr(store, "upsert_document", reclaim_after_write)
    fence = IngestionFence(claim.id, claim.lease_owner, claim.attempts)
    assert asyncio.run(process_document(document_id, fence)) is False
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        assert document is not None and document.status == DocumentStatus.PROCESSING
        assert document.chunk_count == 0
    records, _ = store.client.scroll(
        store.collection_name,
        scroll_filter=store._field_filter("document_id", document_id),
        limit=10,
    )
    assert records == []


def test_expired_final_ingestion_attempt_becomes_dead(client: TestClient, admin_token: str, tmp_path):
    admin_id = client.get("/api/v1/auth/me", headers=auth(admin_token)).json()["id"]
    source = tmp_path / "exhausted.md"
    source.write_text("exhausted", encoding="utf-8")
    with SessionLocal() as db:
        base = KnowledgeBase(name="exhausted-base", description="queue test")
        db.add(base)
        db.flush()
        document = KnowledgeDocument(
            knowledge_base_id=base.id,
            original_name=source.name,
            storage_path=str(source),
            content_type="text/markdown",
            size_bytes=source.stat().st_size,
            sha256="b" * 64,
            status=DocumentStatus.PROCESSING,
            uploaded_by=admin_id,
        )
        db.add(document)
        db.flush()
        job = KnowledgeIngestionJob(
            document_id=document.id,
            status=IngestionJobStatus.RUNNING,
            attempts=2,
            max_attempts=2,
            lease_owner="dead-worker",
            lease_expires_at=utcnow() - timedelta(seconds=1),
        )
        db.add(job)
        db.flush()
        job_id = job.id
        document_id = document.id
        db.commit()

    assert claim_ingestion_job("replacement-worker") is None
    with SessionLocal() as db:
        job = db.get(KnowledgeIngestionJob, job_id)
        document = db.get(KnowledgeDocument, document_id)
        assert job is not None and job.status == IngestionJobStatus.DEAD
        assert job.lease_owner is None
        assert document is not None and document.status == DocumentStatus.FAILED
        assert "最后一次尝试" in (document.error_message or "")
