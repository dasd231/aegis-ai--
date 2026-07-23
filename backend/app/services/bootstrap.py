from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import BACKEND_DIR, get_settings
from app.core.security import hash_password
from app.database import SessionLocal
from app.models import DocumentStatus, KnowledgeBase, KnowledgeDocument, User, UserRole
from app.services.knowledge import _delete_from_all_indexes, process_document, reindex_ready_document
from app.services.job_queue import enqueue_ingestion
from app.services.vector_store import configured_vector_pools, get_vector_store


logger = logging.getLogger(__name__)

SEED_KNOWLEDGE_BASE_NAME = "小米智能客服演示知识库"
LEGACY_SEED_KNOWLEDGE_BASE_NAME = "示例企业知识库"
SEED_DESCRIPTION = "小米产品、订单、售后与排障演示数据（非小米官方生产知识库）"


async def _retire_stale_seed_documents(
    knowledge_base_id: str,
    current_manifest: dict[str, str],
) -> None:
    """Remove superseded built-in material without touching user uploads.

    MySQL remains the authority for retrieval visibility: when vector cleanup
    fails, the stale document is marked failed before the replacement is used.
    This prevents mixed policy generations while leaving the failure visible to
    operators for reconciliation.
    """
    settings = get_settings()
    with SessionLocal() as db:
        snapshots = list(
            db.execute(
                select(
                    KnowledgeDocument.id,
                    KnowledgeDocument.original_name,
                    KnowledgeDocument.sha256,
                    KnowledgeDocument.storage_path,
                ).where(
                    KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                    KnowledgeDocument.is_seed.is_(True),
                )
            ).all()
        )
    stale = [
        row
        for row in snapshots
        if current_manifest.get(row.original_name) != row.sha256
    ]
    for snapshot in stale:
        vector_deleted = True
        try:
            await _delete_from_all_indexes(snapshot.id)
        except Exception:
            vector_deleted = False
            logger.exception("stale seed vector cleanup failed for %s", snapshot.id)
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, snapshot.id)
            if document is None:
                continue
            if not vector_deleted:
                document.status = DocumentStatus.FAILED
                document.error_message = "旧版内嵌演示材料清理失败，已停止参与检索"
                db.commit()
                continue
            file_path = Path(document.storage_path)
            db.delete(document)
            db.commit()
            try:
                file_path.resolve().relative_to(settings.upload_dir.resolve())
                file_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                logger.warning("stale seed file cleanup failed: %s", file_path)


async def reconcile_incomplete_documents() -> None:
    """Make vectors from an interrupted ingestion unreachable and observable."""
    settings = get_settings()
    with SessionLocal() as db:
        statuses = [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]
        if settings.ingestion_mode == "inline":
            statuses.append(DocumentStatus.PROCESSING)
        snapshots = list(
            db.execute(
                select(KnowledgeDocument.id, KnowledgeDocument.status).where(
                    KnowledgeDocument.status.in_(statuses)
                )
            ).all()
        )
    for document_id, prior_status in snapshots:
        vector_deleted = True
        try:
            await _delete_from_all_indexes(document_id)
        except Exception:
            vector_deleted = False
            logger.exception("startup vector reconciliation failed for %s", document_id)
        with SessionLocal() as db:
            document = db.get(KnowledgeDocument, document_id)
            if document is None:
                continue
            if prior_status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED} and vector_deleted:
                file_path = Path(document.storage_path)
                db.delete(document)
                db.commit()
                try:
                    file_path.resolve().relative_to(settings.upload_dir.resolve())
                    file_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    logger.warning("startup orphan file cleanup failed: %s", file_path)
            elif document.status == DocumentStatus.PROCESSING:
                document.status = DocumentStatus.FAILED
                document.error_message = "上次处理被服务重启中断，可在管理端重试"
                db.commit()


async def reconcile_ready_index_generations() -> None:
    """Backfill a newly configured physical vector generation in inline mode.

    The active document rows stay READY, so the previous generation remains
    queryable while each document is embedded into both configured pools.
    Distributed deployments run the explicit reindex job before raising the
    canary percentage instead of making every API replica race at startup.
    """
    settings = get_settings()
    if settings.ingestion_mode != "inline":
        return
    with SessionLocal() as db:
        document_ids = list(
            db.scalars(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.status == DocumentStatus.READY
                )
            ).all()
        )
    pools = configured_vector_pools()
    for document_id in document_ids:
        present = await asyncio.gather(
            *(get_vector_store(pool).contains_document(document_id) for pool in pools)
        )
        missing_pools = [pool for pool, exists in zip(pools, present, strict=True) if not exists]
        if missing_pools:
            await reindex_ready_document(document_id, pools=missing_pools)


async def ensure_seed_data() -> None:
    settings = get_settings()
    await reconcile_incomplete_documents()
    if not settings.auto_seed:
        await reconcile_ready_index_generations()
        return
    sample_dir = BACKEND_DIR / "sample_data"
    sources = [
        source
        for source in sorted(sample_dir.glob("*"))
        if source.is_file() and source.suffix.lower() in settings.extension_set
    ]
    manifest = {
        source.name: hashlib.sha256(source.read_bytes()).hexdigest()
        for source in sources
    }
    with SessionLocal() as db:
        email = settings.initial_admin_email.lower()
        admin = db.scalar(select(User).where(User.email == email))
        if admin is None:
            admin = User(
                email=email,
                password_hash=hash_password(settings.initial_admin_password.get_secret_value()),
                display_name="系统管理员",
                role=UserRole.ADMIN,
            )
            db.add(admin)
            db.flush()
        knowledge_base = db.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == SEED_KNOWLEDGE_BASE_NAME)
        )
        legacy_knowledge_base = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.name == LEGACY_SEED_KNOWLEDGE_BASE_NAME
            )
        )
        legacy_seed_knowledge_base_id: str | None = None
        if knowledge_base is None:
            knowledge_base = legacy_knowledge_base
            if knowledge_base is None:
                knowledge_base = KnowledgeBase(
                    name=SEED_KNOWLEDGE_BASE_NAME,
                    description=SEED_DESCRIPTION,
                )
                db.add(knowledge_base)
                db.flush()
            else:
                knowledge_base.name = SEED_KNOWLEDGE_BASE_NAME
                knowledge_base.description = SEED_DESCRIPTION
        elif legacy_knowledge_base is not None:
            # A partially completed/manual migration may leave both names.
            # Retire only managed seed documents from the legacy base; any user
            # uploads in it remain untouched and visible to administrators.
            legacy_seed_knowledge_base_id = legacy_knowledge_base.id
        admin_id = admin.id
        knowledge_base_id = knowledge_base.id
        db.commit()

    await _retire_stale_seed_documents(knowledge_base_id, manifest)
    if legacy_seed_knowledge_base_id is not None:
        await _retire_stale_seed_documents(legacy_seed_knowledge_base_id, {})

    with SessionLocal() as db:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        pending_ids: list[str] = []
        for source in sources:
            content = source.read_bytes()
            sha256 = manifest[source.name]
            exists = db.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                    KnowledgeDocument.sha256 == sha256,
                    KnowledgeDocument.status.in_([DocumentStatus.PROCESSING, DocumentStatus.READY]),
                )
            )
            if exists:
                if exists.is_seed:
                    exists.original_name = source.name
                continue
            destination = settings.upload_dir / f"{uuid.uuid4()}{source.suffix.lower()}"
            shutil.copyfile(source, destination)
            document = KnowledgeDocument(
                knowledge_base_id=knowledge_base_id,
                original_name=source.name,
                storage_path=str(destination.resolve()),
                content_type="text/markdown" if source.suffix.lower() == ".md" else "text/plain",
                size_bytes=len(content),
                sha256=sha256,
                status=DocumentStatus.PROCESSING,
                is_seed=True,
                uploaded_by=admin_id,
            )
            db.add(document)
            db.flush()
            pending_ids.append(document.id)
            if settings.ingestion_mode == "queue":
                enqueue_ingestion(db, document.id)
        db.commit()
    if settings.ingestion_mode == "inline":
        for document_id in pending_ids:
            await process_document(document_id)
        await reconcile_ready_index_generations()
