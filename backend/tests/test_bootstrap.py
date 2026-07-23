from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

from app.core.config import get_settings
from app.core.security import hash_password
from app.database import SessionLocal
from app.models import DocumentStatus, KnowledgeBase, KnowledgeDocument, User, UserRole
from app.services import bootstrap
from app.services import knowledge
from app.services.chunking import TextChunk


def _document(
    knowledge_base_id: str,
    user_id: str,
    path: Path,
    *,
    name: str,
    digest: str,
    is_seed: bool,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        knowledge_base_id=knowledge_base_id,
        original_name=name,
        storage_path=str(path),
        content_type="text/markdown",
        size_bytes=path.stat().st_size,
        sha256=digest,
        status=DocumentStatus.READY,
        is_seed=is_seed,
        uploaded_by=user_id,
    )


def _seed_replacement_fixture() -> tuple[str, dict[str, str], dict[str, Path]]:
    upload_dir = get_settings().upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex
    paths = {
        "stale": upload_dir / f"stale-{suffix}.md",
        "current": upload_dir / f"current-{suffix}.md",
        "user": upload_dir / f"user-{suffix}.md",
    }
    for label, path in paths.items():
        path.write_text(label, encoding="utf-8")
    with SessionLocal() as db:
        user = User(
            email=f"seed-{suffix}@example.com",
            password_hash=hash_password("SeedTest123!"),
            display_name="Seed test",
            role=UserRole.ADMIN,
        )
        knowledge_base = KnowledgeBase(name=f"seed-kb-{suffix}", description="test")
        db.add_all([user, knowledge_base])
        db.flush()
        db.add_all(
            [
                _document(
                    knowledge_base.id,
                    user.id,
                    paths["stale"],
                    name="old.md",
                    digest="a" * 64,
                    is_seed=True,
                ),
                _document(
                    knowledge_base.id,
                    user.id,
                    paths["current"],
                    name="current.md",
                    digest="b" * 64,
                    is_seed=True,
                ),
                _document(
                    knowledge_base.id,
                    user.id,
                    paths["user"],
                    name="user-upload.md",
                    digest="c" * 64,
                    is_seed=False,
                ),
            ]
        )
        db.commit()
        return knowledge_base.id, {"current.md": "b" * 64}, paths


def test_seed_replacement_removes_only_superseded_managed_material(monkeypatch):
    knowledge_base_id, manifest, paths = _seed_replacement_fixture()
    delete_document = AsyncMock()
    monkeypatch.setattr(bootstrap, "_delete_from_all_indexes", delete_document)

    asyncio.run(bootstrap._retire_stale_seed_documents(knowledge_base_id, manifest))

    with SessionLocal() as db:
        documents = {
            item.original_name: item
            for item in db.query(KnowledgeDocument).filter_by(
                knowledge_base_id=knowledge_base_id
            )
        }
    assert set(documents) == {"current.md", "user-upload.md"}
    assert documents["user-upload.md"].is_seed is False
    delete_document.assert_awaited_once()
    assert not paths["stale"].exists()
    assert paths["current"].exists() and paths["user"].exists()


def test_seed_replacement_failure_disables_stale_document_before_new_ingestion(monkeypatch):
    knowledge_base_id, manifest, paths = _seed_replacement_fixture()
    delete_document = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    monkeypatch.setattr(bootstrap, "_delete_from_all_indexes", delete_document)

    asyncio.run(bootstrap._retire_stale_seed_documents(knowledge_base_id, manifest))

    with SessionLocal() as db:
        stale = db.query(KnowledgeDocument).filter_by(
            knowledge_base_id=knowledge_base_id,
            original_name="old.md",
        ).one()
        user_upload = db.query(KnowledgeDocument).filter_by(
            knowledge_base_id=knowledge_base_id,
            original_name="user-upload.md",
        ).one()
    assert stale.status == DocumentStatus.FAILED
    assert stale.error_message == "旧版内嵌演示材料清理失败，已停止参与检索"
    assert user_upload.status == DocumentStatus.READY
    assert paths["stale"].exists()


def test_ready_generation_repair_only_writes_missing_canary_pool(monkeypatch):
    knowledge_base_id, _, _ = _seed_replacement_fixture()

    class Store:
        def __init__(self, pool: str):
            self.pool = pool

        async def contains_document(self, _document_id: str) -> bool:
            return self.pool == "stable"

    stores = {pool: Store(pool) for pool in ("stable", "canary")}
    reindex = AsyncMock(return_value=True)
    monkeypatch.setattr(bootstrap, "configured_vector_pools", lambda: ("stable", "canary"))
    monkeypatch.setattr(bootstrap, "get_vector_store", lambda pool: stores[pool])
    monkeypatch.setattr(bootstrap, "reindex_ready_document", reindex)

    asyncio.run(bootstrap.reconcile_ready_index_generations())

    assert reindex.await_count == 3
    assert all(call.kwargs["pools"] == ["canary"] for call in reindex.await_args_list)
    assert knowledge_base_id


def test_canary_reindex_failure_never_deletes_existing_stable_generation(monkeypatch):
    class Store:
        def __init__(self, fail: bool = False):
            self.fail = fail
            self.delete_document = AsyncMock()

        async def upsert_document(self, *_args, **_kwargs):
            if self.fail:
                raise RuntimeError("canary unavailable")

    stable = Store()
    canary = Store(fail=True)
    stores = {"stable": stable, "canary": canary}
    monkeypatch.setattr(knowledge, "get_vector_store", lambda pool: stores[pool])
    chunks = [TextChunk(0, "退款规则", 1, "退款", "P0", "退款规则", "退款")]

    try:
        asyncio.run(
            knowledge._upsert_all_indexes(
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "规则.md",
                chunks,
                pools=("stable", "canary"),
                cleanup_successful_on_failure=False,
            )
        )
    except RuntimeError as exc:
        assert "1 index pool" in str(exc)
    else:
        raise AssertionError("canary failure must remain observable")

    stable.delete_document.assert_not_awaited()
    canary.delete_document.assert_not_awaited()
